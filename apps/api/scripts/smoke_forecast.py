"""Smoke test for the Kronos forecast stack.

Online (default) — run: uv run python scripts/smoke_forecast.py [SYMBOL]
  Pulls real daily bars via the same yfinance ingester the API uses, writes
  them into a throwaway DuckDB, runs KronosForecastService with the real
  pretrained weights (first run downloads ~100MB from HuggingFace), and
  sanity-checks the output. Add --variant base|small|mini to exercise a
  specific registry checkpoint (see app/forecast/service.py VARIANTS)
  instead of the settings-configured default; omit it for identical
  behavior to before this flag existed.

Offline — run: uv run python scripts/smoke_forecast.py --offline
  For sandboxes/CI where huggingface.co and Yahoo are unreachable: seeds the
  DB from scripts/data/kronos_smoke_input.csv (real 5-min OHLCV bars vendored
  from upstream's regression fixtures) and runs a randomly-initialized
  Kronos architecture saved to a temp dir. Values are meaningless but every
  line of the integration — tokenizer, autoregressive sampling, service,
  normalization round-trip — executes for real. --variant is ignored here:
  the offline path always uses its own local random checkpoint.

Exit code 0 = every check passed.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OFFLINE = "--offline" in sys.argv
argv = list(sys.argv[1:])
VARIANT: str | None = None
if "--variant" in argv:
    i = argv.index("--variant")
    VARIANT = argv[i + 1]
    del argv[i : i + 2]  # drop the flag + its value before positional parsing
args = [a for a in argv if not a.startswith("--")]
SYMBOL = args[0] if args else ("SMOKE" if OFFLINE else "SPY")
HORIZON = 30
FIXTURE = Path(__file__).parent / "data" / "kronos_smoke_input.csv"

INSERT_SQL = (
    "INSERT OR REPLACE INTO ts_price "
    "(source, symbol, asset_class, ts, open, high, low, close, volume) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def seed_yfinance(duck) -> int:
    import yfinance as yf

    hist = yf.Ticker(SYMBOL).history(period="2y", auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"yfinance returned no history for {SYMBOL}")
    rows = [
        (
            "yahoo", SYMBOL, "equity", ts.to_pydatetime().replace(tzinfo=None),
            float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]),
            float(r["Volume"]),
        )
        for ts, r in hist.iterrows()
    ]
    duck.executemany(INSERT_SQL, rows)
    return len(rows)


def seed_fixture(duck) -> int:
    import pandas as pd

    df = pd.read_csv(FIXTURE, parse_dates=["timestamps"])
    rows = [
        (
            "yahoo", SYMBOL, "equity", r["timestamps"].to_pydatetime(),
            float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
            float(r["volume"]),
        )
        for _, r in df.iterrows()
    ]
    duck.executemany(INSERT_SQL, rows)
    return len(rows)


def build_random_checkpoints(tmp: Path) -> tuple[str, str]:
    """Tiny randomly-initialized Kronos + tokenizer, saved as local HF dirs."""
    from app.forecast.kronos import Kronos, KronosTokenizer

    tokenizer = KronosTokenizer(
        d_in=6, d_model=64, n_heads=4, ff_dim=128, n_enc_layers=2, n_dec_layers=2,
        ffn_dropout_p=0.0, attn_dropout_p=0.0, resid_dropout_p=0.0,
        s1_bits=8, s2_bits=8, beta=1.0, gamma0=1.0, gamma=1.0, zeta=1.0, group_size=4,
    )
    model = Kronos(
        s1_bits=8, s2_bits=8, n_layers=2, d_model=64, n_heads=4, ff_dim=128,
        ffn_dropout_p=0.0, attn_dropout_p=0.0, resid_dropout_p=0.0,
        token_dropout_p=0.0, learn_te=True,
    )
    tok_dir, model_dir = tmp / "tokenizer", tmp / "model"
    tokenizer.save_pretrained(tok_dir)
    model.save_pretrained(model_dir)
    return str(model_dir), str(tok_dir)


async def main() -> int:
    from app.db.duck import DuckStore
    from app.db.schema import init_duckdb
    from app.forecast import KronosForecastService

    with tempfile.TemporaryDirectory() as tmp:
        duck = DuckStore(Path(tmp) / "smoke.duckdb")
        init_duckdb(duck)

        n = seed_fixture(duck) if OFFLINE else seed_yfinance(duck)
        print(f"seeded {n} real {SYMBOL} bars ({'vendored fixture' if OFFLINE else 'yfinance'})")

        dist_paths = 4 if OFFLINE else 8
        if OFFLINE:
            model_id, tokenizer_id = build_random_checkpoints(Path(tmp))
            print("offline: random-weight Kronos (integration only — values are noise)")
            service = KronosForecastService(duck, model_id=model_id, tokenizer_id=tokenizer_id)
            result = await service.forecast(SYMBOL, "equity", horizon=HORIZON)
            t0 = time.monotonic()
            dist_result = await service.forecast_distribution(
                SYMBOL, "equity", horizon=HORIZON, paths=dist_paths
            )
            dist_wall_s = time.monotonic() - t0
        else:
            service = KronosForecastService(duck)
            if VARIANT:
                print(f"variant: {VARIANT}")
            result = await service.forecast(SYMBOL, "equity", horizon=HORIZON, variant=VARIANT)
            t0 = time.monotonic()
            dist_result = await service.forecast_distribution(
                SYMBOL, "equity", horizon=HORIZON, paths=dist_paths, variant=VARIANT
            )
            dist_wall_s = time.monotonic() - t0
        service.close()

    last_close = result.history[-1].close
    checks = {
        f"forecast has {HORIZON} bars": len(result.forecast) == HORIZON,
        "history tail present": len(result.history) > 0,
        "all values finite": all(
            all(v == v and abs(v) != float("inf") for v in (b.open, b.high, b.low, b.close, b.volume))
            for b in result.forecast
        ),
        "volume non-negative": all(b.volume >= 0 for b in result.forecast),
        "timestamps strictly ascending": all(
            a.t < b.t for a, b in zip(result.forecast, result.forecast[1:])
        ),
        "forecast continues after history": result.forecast[0].t > result.history[-1].t,
    }
    if not OFFLINE:  # value-level sanity only makes sense with trained weights
        checks["OHLC all positive"] = all(
            min(b.open, b.high, b.low, b.close) > 0 for b in result.forecast
        )
        checks["first bar within 25% of last close"] = (
            abs(result.forecast[0].close / last_close - 1) < 0.25
        )

    print(f"\nmodel={result.model_id} device={result.device} context={result.context_bars} bars")
    print(f"last real close {last_close:.2f} → forecast closes "
          f"[{result.forecast[0].close:.2f} … {result.forecast[-1].close:.2f}]")

    # --- distribution mode: N-path ensemble quantile cone + stats -------------
    dist_probs = (
        [dist_result.stats.p_up, dist_result.stats.p_dd_5, dist_result.stats.p_dd_10]
        + [lv.p_touch for lv in dist_result.levels]
    )
    dist_last_hist_t = dist_result.history[-1].t
    checks[f"distribution: {dist_paths} paths"] = dist_result.paths == dist_paths
    checks["distribution: quantile arrays length == horizon"] = len(dist_result.quantiles) == HORIZON
    checks["distribution: p10 <= p50 <= p90 elementwise"] = all(
        q.p10 <= q.p50 <= q.p90 for q in dist_result.quantiles
    )
    checks["distribution: p10 <= p25 <= p50 <= p75 <= p90 elementwise"] = all(
        q.p10 <= q.p25 <= q.p50 <= q.p75 <= q.p90 for q in dist_result.quantiles
    )
    checks["distribution: all probs in [0, 1]"] = all(0.0 <= p <= 1.0 for p in dist_probs)
    checks["distribution: quantile timestamps strictly ascending"] = all(
        a.t < b.t for a, b in zip(dist_result.quantiles, dist_result.quantiles[1:])
    )
    checks["distribution: quantile timestamps after history"] = (
        dist_result.quantiles[0].t > dist_last_hist_t
    )

    last_q = dist_result.quantiles[-1]
    dist_last_close = dist_result.history[-1].close
    cone_width_pct = (last_q.p90 - last_q.p10) / dist_last_close * 100
    print(
        f"\ndistribution: paths={dist_result.paths} wall={dist_wall_s:.2f}s device={dist_result.device}"
    )
    print(
        f"  p_up={dist_result.stats.p_up:.3f} median_return={dist_result.stats.median_return:+.4f} "
        f"mean_return={dist_result.stats.mean_return:+.4f} std_return={dist_result.stats.std_return:.4f} "
        f"skew={dist_result.stats.skew_return:+.3f}"
    )
    print(
        f"  terminal close p10={last_q.p10:.2f} p50={last_q.p50:.2f} p90={last_q.p90:.2f} "
        f"cone_width={cone_width_pct:.2f}% of last close"
    )
    print(
        f"  median_max_drawdown={dist_result.stats.median_max_drawdown:.4f} "
        f"p_dd_5={dist_result.stats.p_dd_5:.3f} p_dd_10={dist_result.stats.p_dd_10:.3f}"
    )
    for lv in dist_result.levels:
        print(f"  level {lv.level:.2f} ({lv.direction}): p_touch={lv.p_touch:.3f}")

    ok = True
    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok &= passed
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
