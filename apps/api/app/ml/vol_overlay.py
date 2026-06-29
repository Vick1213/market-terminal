"""Volatility-targeted exposure overlay — the deployable, defensive use of the one
signal that is genuinely forecastable on this data (PLAN §12 follow-through).

§12 + the vol-baseline check established: returns are NOT forecastable after
deflation, but forward realised vol IS (and trailing Garman-Klass vol already
nails it — IC 0.53/0.70). This module converts that into the one honest edge
available: NOT alpha, but *risk control*. Exposure is scaled inversely to the
GK-vol forecast and cut in a high-vol/down-trend regime, so the book is small
going into the volatility clusters that precede drawdowns.

This is descriptive/defensive, held to honest standards: the backtest reports
net-of-cost Sharpe, max drawdown, Calmar, AND a per-subperiod breakdown (a single
vol-target rule has ~no free parameters to overfit, but we still show it isn't a
one-regime artifact). It does not claim to beat the market on return — it claims a
better *risk-adjusted* path, which is what vol-timing actually delivers
(Moreira-Muir 2017; Shu-Yu-Mulvey 2024 regime gate halves drawdown).

`current_signal()` exposes the latest regime + suggested exposure for the API /
strategist wiring.

Run:  cd apps/api && .venv/bin/python -m app.ml.vol_overlay [--symbol SPY --target-vol 0.15]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

_LN2 = np.log(2.0)
_YEAR = 252.0


def _market_db() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "market.duckdb"


def _load_ohlc(symbol: str, duck=None) -> pd.DataFrame:
    """Load OHLC. ``duck`` = an existing fetchall-capable store (the live API's
    DuckStore) — reused so we don't open a 2nd connection to the single-writer
    market.duckdb in-process. ``None`` (CLI) opens its own read-only connection."""
    sql = ("SELECT ts, open, high, low, close FROM ts_price WHERE symbol=? "
           "AND open>0 AND high>0 AND low>0 AND close>0 ORDER BY ts")
    if duck is not None:
        rows = duck.fetchall(sql, [symbol])
    else:
        con = duckdb.connect(str(_market_db()), read_only=True)
        rows = con.execute(sql, [symbol]).fetchall()
        con.close()
    idx = pd.to_datetime([r[0] for r in rows]).normalize()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"], index=idx)
    return df.drop(columns="ts").astype(float)


def _gk_daily_vol(df: pd.DataFrame) -> pd.Series:
    """Garman-Klass daily volatility from OHLC (efficient, no intraday needed)."""
    hl = np.log(df.high / df.low) ** 2
    co = np.log(df.close / df.open) ** 2
    var = (0.5 * hl - (2.0 * _LN2 - 1.0) * co).clip(lower=1e-8)
    return np.sqrt(var)


def build_signals(df: pd.DataFrame, *, vol_win: int = 20, target_vol: float = 0.15,
                  max_w: float = 1.0, regime_pct: float = 0.80,
                  trend_win: int = 200) -> pd.DataFrame:
    """Per-day: forecast vol, vol-target weight, regime flag, gated weight.

    All inputs are trailing/causal → the weight at day t is knowable at the close
    of t and applied to the return of t+1 (no look-ahead).
    """
    out = pd.DataFrame(index=df.index)
    out["ret"] = np.log(df.close).diff()
    gk = _gk_daily_vol(df)
    out["fvol"] = gk.rolling(vol_win, min_periods=vol_win // 2).mean() * np.sqrt(_YEAR)
    # vol target: hold less when forecast vol exceeds target; cap at max_w (de-risk only).
    out["w_voltarget"] = (target_vol / out["fvol"]).clip(upper=max_w).clip(lower=0.0)
    # regime gate: stress if forecast vol is in its own high tail (expanding pct, PIT)
    # AND price is below its long trend. Cut to flat in stress.
    pct = out["fvol"].expanding(min_periods=252).apply(
        lambda x: (x[-1] >= np.nanpercentile(x, regime_pct * 100)), raw=True)
    sma = df.close.rolling(trend_win, min_periods=trend_win // 2).mean()
    out["stress"] = ((pct == 1.0) & (df.close < sma)).astype(float)
    out["w_regime"] = np.where(out["stress"] > 0, 0.0, out["w_voltarget"])
    return out


def _equity_metrics(strat_ret: pd.Series) -> dict:
    r = strat_ret.dropna()
    if len(r) < 60:
        return {}
    eq = (1.0 + r).cumprod()
    yrs = len(r) / _YEAR
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = r.std() * np.sqrt(_YEAR)
    sharpe = (r.mean() * _YEAR) / vol if vol > 0 else np.nan
    dd = (eq / eq.cummax() - 1.0).min()
    calmar = cagr / abs(dd) if dd < 0 else np.nan
    return {"cagr": cagr, "vol": vol, "sharpe": sharpe, "maxdd": dd, "calmar": calmar}


def _apply(sig: pd.DataFrame, wcol: str, cost_bps: float) -> tuple[pd.Series, dict]:
    """Lag weight by 1 day (decide at close t, hold t+1), net of turnover cost."""
    w = sig[wcol].shift(1).fillna(0.0)
    turn = w.diff().abs().fillna(0.0)
    sr = w * (np.exp(sig["ret"]) - 1.0) - turn * (cost_bps / 1e4)
    m = _equity_metrics(sr)
    m["avg_w"] = float(w.mean())
    m["turnover_yr"] = float(turn.sum() / (len(turn) / _YEAR))
    return sr, m


def backtest(symbol: str = "SPY", target_vol: float = 0.15, cost_bps: float = 1.0,
             max_w: float = 1.0, duck=None, verbose: bool = True) -> dict:
    df = _load_ohlc(symbol, duck=duck)
    sig = build_signals(df, target_vol=target_vol, max_w=max_w)
    bh = sig["ret"].apply(lambda x: np.exp(x) - 1.0)  # buy & hold simple returns

    strats = {
        "buy_hold": (bh, {**_equity_metrics(bh), "avg_w": 1.0, "turnover_yr": 0.0}),
        "vol_target": _apply(sig, "w_voltarget", cost_bps),
        "vol_target+regime": _apply(sig, "w_regime", cost_bps),
    }
    if not verbose:
        return {n: m for n, (_, m) in strats.items()}
    print(f"=== VOL-TARGETED OVERLAY — {symbol}  ({df.index[0].date()}→{df.index[-1].date()},"
          f" {len(df):,} days, target_vol={target_vol:.0%}, cost={cost_bps}bp) ===\n")
    print(f"  {'strategy':20s} {'CAGR':>7} {'Vol':>6} {'Sharpe':>7} {'MaxDD':>7} "
          f"{'Calmar':>7} {'avgW':>6} {'trn/yr':>7}")
    for name, (sr, m) in strats.items():
        print(f"  {name:20s} {m['cagr']:>6.1%} {m['vol']:>6.1%} {m['sharpe']:>7.2f} "
              f"{m['maxdd']:>7.1%} {_n(m.get('calmar')):>7} {m['avg_w']:>6.2f} "
              f"{m['turnover_yr']:>7.1f}")

    # subperiod robustness on the regime strategy vs buy-hold (not a one-era fluke)
    print("\n  --- subperiod Sharpe (vol_target+regime vs buy_hold) ---")
    reg_sr = strats["vol_target+regime"][0]
    yrs = sig.index.year
    for lo in range(int(yrs.min()) // 10 * 10, int(yrs.max()) + 1, 10):
        hi = lo + 9
        mask = (yrs >= lo) & (yrs <= hi)
        if mask.sum() < 250:
            continue
        rs = _equity_metrics(reg_sr[mask]).get("sharpe", np.nan)
        bs = _equity_metrics(bh[mask]).get("sharpe", np.nan)
        print(f"    {lo}-{hi}:  regime {_n(rs,2):>6}  buy-hold {_n(bs,2):>6}")

    bh_m, reg_m = strats["buy_hold"][1], strats["vol_target+regime"][1]
    print(f"\n  VERDICT: regime overlay Sharpe {reg_m['sharpe']:.2f} vs buy-hold "
          f"{bh_m['sharpe']:.2f}  (Δ{reg_m['sharpe']-bh_m['sharpe']:+.2f}); "
          f"maxDD {reg_m['maxdd']:.0%} vs {bh_m['maxdd']:.0%} "
          f"({(1-reg_m['maxdd']/bh_m['maxdd'])*100:+.0f}% drawdown). "
          f"Defensive edge, not alpha.")
    return {n: m for n, (_, m) in strats.items()}


def current_signal(symbol: str = "SPY", target_vol: float = 0.15, max_w: float = 1.0,
                   duck=None) -> dict:
    """Latest regime + suggested exposure — for the API/strategist."""
    df = _load_ohlc(symbol, duck=duck)
    sig = build_signals(df, target_vol=target_vol, max_w=max_w)
    last = sig.dropna(subset=["fvol"]).iloc[-1]
    fvol = float(last["fvol"])
    # Headline = the robust continuous vol-target weight (the hard regime gate
    # underperformed in backtest, so it is NOT the recommended sizing — kept only
    # as informational context).
    return {
        "symbol": symbol,
        "as_of": sig.index[-1].date().isoformat(),
        "forecast_vol_annual": round(fvol, 4),
        "target_vol": target_vol,
        "suggested_exposure": round(float(last["w_voltarget"]), 3),
        "regime": "stress" if last["stress"] > 0 else "normal",
        "vol_percentile": _vol_pctile(sig["fvol"]),
    }


def overlay_payload(duck=None, symbol: str = "SPY", target_vol: float = 0.15) -> dict:
    """API payload: latest signal + backtest summary (defensive-edge metrics)."""
    sig = current_signal(symbol, target_vol, duck=duck)
    bt = backtest(symbol, target_vol, duck=duck, verbose=False)
    def keep(m):
        return {k: round(float(v), 4) for k, v in m.items()
                if isinstance(v, (int, float)) and np.isfinite(v)}
    return {"signal": sig, "backtest": {n: keep(m) for n, m in bt.items()}}


def _vol_pctile(fvol: pd.Series) -> float:
    s = fvol.dropna()
    return round(float((s <= s.iloc[-1]).mean()), 3) if len(s) else float("nan")


def _n(x, d=2):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{d}f}"


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--target-vol", type=float, default=0.15)
    ap.add_argument("--cost-bps", type=float, default=1.0)
    ap.add_argument("--max-w", type=float, default=1.0)
    a = ap.parse_args()
    backtest(a.symbol, a.target_vol, a.cost_bps, a.max_w)
    print("\ncurrent:", current_signal(a.symbol, a.target_vol, a.max_w))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
