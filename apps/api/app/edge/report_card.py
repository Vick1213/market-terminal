"""Strategist report card — does the engine actually carry information?

Pure read-time compute on stored data (no network, blocking — run_in_executor):

  1. Snapshot scoring: every stored strategist snapshot's allocation is scored
     against realized forward returns (1w / 1m of trading days) built from the
     daily bars the terminal already stores, vs SPY and a 60/40-style mix.
     Holdings-level when the snapshot has them (sector ETFs, single names,
     GLD/SLV, BTC/ETH), bucket-proxy fallback for symbols with no bars.
  2. Regime backtest: the PLAN §4 regime classifier REPLAYED over the stored
     macro series history (vectorized mirror of composite._classify_regime —
     same dials, same thresholds, staleness relative to each as-of day), each
     day mapped through BASE_ALLOCATION (the rules table, without tilts) and
     scored the same way. This fills the card with years of evidence from day
     one, before live snapshots accumulate.
  3. Signal hit rates: per signal key, forward SPY returns after days the
     signal fired a tilt vs days it did not — a signal that doesn't separate
     the two carries no information and should be re-sized or dropped.

Cash is scored at 0% return (T-bill carry ignored), which slightly UNDERSTATES
defensive allocations — stated in the response so the reader can adjust.
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left
from datetime import datetime

import pandas as pd

from app.db.duck import DuckStore
from app.macro.composite import Z_CLAMP, _derive, _load_series

log = logging.getLogger("market.edge.report_card")

HORIZONS = {"1w": 5, "1m": 21}  # trading days

# Bucket -> proxy symbol when a snapshot predates holdings or bars are missing.
_BUCKET_PROXY = {"equities": "SPY", "metals": "GLD", "crypto": "BTC/USD"}

# Crypto holdings are stored as bare BTC/ETH; bars live under the pair symbol.
_SYMBOL_ALIAS = {"BTC": "BTC/USD", "ETH": "ETH/USD"}


class _Prices:
    """Lazy per-symbol daily close series with forward-return lookup."""

    def __init__(self, duck: DuckStore) -> None:
        self._duck = duck
        self._series: dict[str, tuple[list[datetime], list[float]]] = {}

    def _load(self, symbol: str) -> tuple[list[datetime], list[float]]:
        if symbol not in self._series:
            rows = self._duck.fetchall(
                "SELECT ts, close FROM ts_price WHERE source = 'yahoo' "
                "AND symbol = ? AND close IS NOT NULL ORDER BY ts",
                [symbol],
            )
            self._series[symbol] = ([r[0] for r in rows], [float(r[1]) for r in rows])
        return self._series[symbol]

    def forward_return(self, symbol: str, asof: datetime, bars: int) -> float | None:
        """Pct return from the first bar ON/AFTER ``asof`` to ``bars`` bars later."""
        symbol = _SYMBOL_ALIAS.get(symbol, symbol)
        ts, close = self._load(symbol)
        i = bisect_left(ts, asof)
        j = i + bars
        if i >= len(ts) or j >= len(ts) or not close[i]:
            return None
        return (close[j] / close[i] - 1) * 100


def _allocation_return(
    prices: _Prices, buckets: list[dict], asof: datetime, bars: int
) -> float | None:
    """Weighted forward return of a snapshot's buckets. Cash returns 0.
    None when no risk sleeve could be priced (avoids fake all-cash zeros)."""
    total, priced_any = 0.0, False
    for b in buckets:
        w = float(b.get("weight_pct") or 0) / 100
        if w <= 0 or b["key"] == "cash":
            continue
        legs = [
            (h["symbol"], float(h.get("sleeve_pct") or 0) / 100)
            for h in b.get("holdings") or []
            if h.get("sleeve_pct")
        ] or [(_BUCKET_PROXY.get(b["key"], "SPY"), 1.0)]
        bucket_ret, covered = 0.0, 0.0
        for sym, share in legs:
            r = prices.forward_return(sym, asof, bars)
            if r is None:  # no bars for a single name — fall back to the proxy
                r = prices.forward_return(_BUCKET_PROXY.get(b["key"], "SPY"), asof, bars)
            if r is None:
                continue
            bucket_ret += share * r
            covered += share
        if covered > 0:
            total += w * bucket_ret / covered
            priced_any = True
    return round(total, 2) if priced_any else None


def _replay_regimes(duck: DuckStore) -> pd.Series:
    """Daily regime series replayed over stored macro history — a vectorized
    mirror of composite._classify_regime (same dials, votes and stress
    overrides), with per-day staleness instead of 'now'. Kept in lockstep
    with that function by hand; divergence shows up as backtest-vs-live
    regime mismatches on recent days."""
    series = _load_series(duck)
    if not series:
        return pd.Series(dtype=object)
    _derive(series)

    def _daily(sid: str, max_gap: int = 35) -> pd.Series | None:
        s = series.get(sid)
        if s is None or s.dropna().empty:
            return None
        s = s.dropna()
        # Forward-fill onto a daily calendar, but never across gaps longer
        # than the staleness cap (mirrors MAX_STALENESS_DAYS).
        idx = pd.date_range(s.index.min(), s.index.max(), freq="D")
        return s.reindex(idx).ffill(limit=max_gap)

    votes: list[pd.Series] = []
    d_real = _daily("DFII10")
    if d_real is not None:
        votes.append((d_real.diff(20) < 0).astype(int) * 2 - 1)
    d_usd = _daily("DTWEXBGS")
    if d_usd is not None:
        votes.append((d_usd.diff(20) < 0).astype(int) * 2 - 1)
    hy = _daily("BAMLH0A0HYM2")
    if hy is not None:
        mu = hy.rolling(730, min_periods=100).mean()
        sd = hy.rolling(730, min_periods=100).std()
        hy_z = ((hy - mu) / sd).clip(-Z_CLAMP, Z_CLAMP)
        votes.append((hy_z < 0).astype(int) * 2 - 1)
    curve = _daily("T10Y2Y")
    if curve is not None:
        votes.append((curve > 0).astype(int) * 2 - 1)
    if not votes:
        return pd.Series(dtype=object)

    frame = pd.concat(votes, axis=1)
    total = frame.sum(axis=1, min_count=1)

    stress = pd.Series(False, index=frame.index)
    term = _daily("VIX_TERM")
    if term is not None:
        stress |= term.reindex(frame.index) < 1.0
    if hy is not None:
        stress |= hy.diff(20).reindex(frame.index) > 0.50

    regimes = pd.Series("neutral", index=frame.index, dtype=object)
    regimes[total >= 2] = "risk-on"
    regimes[total <= -2] = "risk-off"
    regimes[stress.fillna(False)] = "stress"
    regimes[total.isna()] = None
    return regimes.dropna()


def _fired_signals(snapshot: dict) -> set[str]:
    """Signal keys that actually moved an allocation (delta != 0 somewhere)."""
    keys: set[str] = set()
    for b in snapshot.get("buckets") or []:
        for r in b.get("reasons") or []:
            if r.get("delta"):
                keys.add(r["signal"])
    return keys


def compute_report_card(duck: DuckStore, base_allocation: dict[str, dict[str, float]]) -> dict:
    prices = _Prices(duck)
    now = datetime.now()

    # --- 1. score every stored snapshot --------------------------------------
    snap_rows = duck.fetchall(
        "SELECT ts, regime, detail FROM strategist_snapshots ORDER BY ts"
    )
    snapshots: list[dict] = []
    for ts, regime, detail in snap_rows:
        try:
            snap = json.loads(detail) if detail else {}
        except ValueError:
            continue
        entry: dict = {"date": str(ts)[:10], "regime": regime,
                       "fired": sorted(_fired_signals(snap))}
        for label, bars in HORIZONS.items():
            entry[f"ret_{label}"] = _allocation_return(
                prices, snap.get("buckets") or [], ts, bars)
            entry[f"spy_{label}"] = prices.forward_return("SPY", ts, bars)
        snapshots.append(entry)

    scored = [s for s in snapshots if s.get("ret_1w") is not None
              and s.get("spy_1w") is not None]
    summary = {
        "snapshots": len(snapshots),
        "scored": len(scored),
        "avg_excess_1w": round(
            sum(s["ret_1w"] - s["spy_1w"] for s in scored) / len(scored), 2)
        if scored else None,
        "hit_rate_1w": round(
            100 * sum(1 for s in scored if s["ret_1w"] >= s["spy_1w"]) / len(scored))
        if scored else None,
    }

    # --- 2. regime backtest: classifier replayed over macro history ----------
    replayed = _replay_regimes(duck)
    regime_stats: dict[str, dict] = {}
    for ts, regime in replayed.items():
        base = base_allocation.get(regime)
        if base is None:
            continue
        asof = ts.to_pydatetime()
        buckets = [{"key": k, "weight_pct": v, "holdings": []} for k, v in base.items()]
        st = regime_stats.setdefault(
            regime, {"regime": regime, "days": 0,
                     **{f"alloc_{h}": [] for h in HORIZONS},
                     **{f"spy_{h}": [] for h in HORIZONS}},
        )
        st["days"] += 1
        for label, bars in HORIZONS.items():
            r = _allocation_return(prices, buckets, asof, bars)
            spy = prices.forward_return("SPY", asof, bars)
            if r is not None and spy is not None:
                st[f"alloc_{label}"].append(r)
                st[f"spy_{label}"].append(spy)
    regimes_out = []
    for st in regime_stats.values():
        row = {"regime": st["regime"], "days": st["days"]}
        for label in HORIZONS:
            a, s = st[f"alloc_{label}"], st[f"spy_{label}"]
            row[f"n_{label}"] = len(a)
            row[f"alloc_{label}"] = round(sum(a) / len(a), 2) if a else None
            row[f"spy_{label}"] = round(sum(s) / len(s), 2) if s else None
        regimes_out.append(row)
    regimes_out.sort(key=lambda r: -r["days"])

    # --- 3. per-signal hit rates over scored snapshots ------------------------
    signal_keys = sorted({k for s in scored for k in s["fired"]})
    signals_out = []
    for key in signal_keys:
        fired = [s for s in scored if key in s["fired"]]
        quiet = [s for s in scored if key not in s["fired"]]
        signals_out.append({
            "signal": key,
            "fired": len(fired),
            "spy_1w_after_fired": round(
                sum(s["spy_1w"] for s in fired) / len(fired), 2) if fired else None,
            "spy_1w_after_quiet": round(
                sum(s["spy_1w"] for s in quiet) / len(quiet), 2) if quiet else None,
        })

    return {
        "as_of": now.isoformat(timespec="minutes"),
        "summary": summary,
        "snapshots": snapshots[-30:],
        "regimes": regimes_out,
        "signals": signals_out,
        "note": (
            "Forward returns use stored daily bars; cash scored at 0% "
            "(understates defensive mixes by T-bill carry). The regime table "
            "REPLAYS the classifier over stored macro history and applies the "
            "base-allocation rules without tilts; snapshot scoring and signal "
            "hit rates use the live engine and need weeks of snapshots to "
            "mean anything."
        ),
    }
