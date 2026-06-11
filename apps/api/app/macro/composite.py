"""Composite Risk-On/Off score + regime classifier (PLAN §3c, §4).

Pure computation over series already stored in ts_macro: z-score each input
over a trailing ~2y window, sign-align so + = risk-on, average inside each
sub-bucket, then weight-average the buckets into one −100..+100 score. The
sub-bucket contributions are kept so the UI can show *what* drives the regime.

Breadth (20%) has no free API and waits for the Phase-3 price universe; until
its series exist the remaining bucket weights are renormalized, so the score
stays honest instead of silently treating missing data as neutral.

Everything here is blocking (pandas + DuckDB reads) — callers dispatch via
``run_in_executor``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.db.duck import DuckStore

# How far back to load for z-scores (a bit over 2 years of calendar days).
WINDOW_DAYS = 800
# Clamp individual z-scores so one wild print can't dominate a bucket.
Z_CLAMP = 3.0
# A member whose latest observation is older than this is excluded — a frozen
# source (e.g. a dead endpoint still serving old files) must never pose as a
# current reading. 35d accommodates weekly prints + holidays.
MAX_STALENESS_DAYS = 35
# Score = weighted-mean(z) * SCALE, clamped to ±100 (|z|=3 maps to ±100).
SCALE = 100.0 / Z_CLAMP

# (series_id, sign) per bucket — sign +1 means "high = risk-on".
# Weights per PLAN §3c: Volatility 25, Credit 20, FinConditions 20, Breadth 20,
# Sentiment/positioning 15.
BUCKETS: list[dict] = [
    {
        "key": "volatility",
        "label": "Volatility",
        "weight": 0.25,
        "members": [("VIXCLS", -1.0), ("VIX_TERM", +1.0)],
    },
    {
        "key": "credit",
        "label": "Credit",
        "weight": 0.20,
        "members": [("BAMLH0A0HYM2", -1.0), ("BAMLC0A0CM", -1.0)],
    },
    {
        "key": "fin_conditions",
        "label": "Financial conditions",
        "weight": 0.20,
        "members": [("NFCI", -1.0), ("ANFCI", -1.0)],
    },
    {
        "key": "breadth",
        "label": "Breadth",
        "weight": 0.20,
        "members": [("BREADTH_PCT_ABOVE_200DMA", +1.0)],  # arrives with Phase 3
    },
    {
        "key": "sentiment",
        "label": "Sentiment / positioning",
        "weight": 0.15,
        "members": [("NAAIM_EXPOSURE", +1.0), ("AAII_SPREAD", -1.0), ("PC_TOTAL", -1.0)],
    },
]

# Every raw series the engine reads (members + regime dials + derived inputs).
_RAW_SERIES = [
    "VIXCLS", "VIX", "VIX3M",
    "BAMLH0A0HYM2", "BAMLC0A0CM",
    "NFCI", "ANFCI",
    "BREADTH_PCT_ABOVE_200DMA",
    "NAAIM_EXPOSURE", "AAII_BULL", "AAII_BEAR", "PC_TOTAL",
    "DFII10", "DTWEXBGS", "T10Y2Y",
    "WALCL", "RRPONTSYD", "WTREGEN",
    "NET_LIQUIDITY",  # daily ($bn), stored by the Phase-8 liquidity pipeline
]


@dataclass
class Component:
    series_id: str
    sign: float
    value: float
    z: float
    ts: str


@dataclass
class Bucket:
    key: str
    label: str
    weight: float          # planned weight
    weight_used: float     # after renormalizing over available buckets
    z: float | None
    contribution: float    # weight_used * z * SCALE (points of the composite)
    components: list[Component] = field(default_factory=list)


@dataclass
class CompositeResult:
    score: float           # −100..+100, + = risk-on
    regime: str            # risk-on | neutral | risk-off | stress
    computed_at: str
    buckets: list[Bucket]
    dials: dict[str, dict] # regime-classifier inputs, for the UI

    def detail_json(self) -> str:
        return json.dumps(asdict(self))


def _load_series(duck: DuckStore) -> dict[str, pd.Series]:
    """ts_macro rows -> {series_id: date-indexed pd.Series}, last value per day."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    placeholders = ", ".join("?" for _ in _RAW_SERIES)
    df = duck.fetchdf(
        f"SELECT series_id, ts, value FROM ts_macro "
        f"WHERE series_id IN ({placeholders}) AND ts >= ? AND value IS NOT NULL "
        f"ORDER BY ts",
        [*_RAW_SERIES, cutoff.replace(tzinfo=None)],
    )
    out: dict[str, pd.Series] = {}
    if df.empty:
        return out
    df["date"] = pd.to_datetime(df["ts"]).dt.normalize()
    for sid, g in df.groupby("series_id"):
        out[sid] = g.groupby("date")["value"].last().sort_index()
    return out


def _derive(series: dict[str, pd.Series]) -> None:
    """Derived inputs: VIX term structure, AAII spread, Fed net liquidity."""
    vix = series.get("VIXCLS") if "VIXCLS" in series else series.get("VIX")
    vix3m = series.get("VIX3M")
    if vix is not None and vix3m is not None:
        ratio = (vix3m / vix).dropna()
        if not ratio.empty:
            series["VIX_TERM"] = ratio
    if "VIXCLS" not in series and "VIX" in series:
        series["VIXCLS"] = series["VIX"]  # CBOE close fills in if FRED is missing
    bull, bear = series.get("AAII_BULL"), series.get("AAII_BEAR")
    if bull is not None and bear is not None:
        spread = (bull - bear).dropna()
        if not spread.empty:
            series["AAII_SPREAD"] = spread
    walcl, rrp, tga = series.get("WALCL"), series.get("RRPONTSYD"), series.get("WTREGEN")
    if walcl is not None and rrp is not None and tga is not None:
        # Weekly FRED proxy in $bn (WALCL/WTREGEN are $mn; RRPONTSYD is $bn).
        # Mixed cadences (weekly/daily) — align on a union calendar and ffill.
        idx = walcl.index.union(rrp.index).union(tga.index)
        net = (
            walcl.reindex(idx).ffill() / 1000.0
            - rrp.reindex(idx).ffill()
            - tga.reindex(idx).ffill() / 1000.0
        ).dropna()
        # The Phase-8 pipeline stores a daily NET_LIQUIDITY ($bn) from
        # FiscalData TGA + NY Fed RRP — splice it over the weekly proxy so
        # recent data is daily and pre-2022 history is preserved.
        daily = series.get("NET_LIQUIDITY")
        if daily is not None and not daily.dropna().empty:
            daily = daily.dropna()
            net = pd.concat([net[net.index < daily.index.min()], daily])
        if not net.empty:
            series["NET_LIQUIDITY"] = net


def _zscore(s: pd.Series) -> tuple[float, float, str] | None:
    """(latest value, clamped z vs trailing window, latest ts) or None —
    None also when the latest observation is too stale to call "current"."""
    s = s.dropna()
    if len(s) < 20:  # too little history for a meaningful z
        return None
    age = pd.Timestamp.now().normalize() - s.index[-1]
    if age.days > MAX_STALENESS_DAYS:
        return None
    std = float(s.std())
    if not std or pd.isna(std):
        return None
    z = (float(s.iloc[-1]) - float(s.mean())) / std
    z = max(-Z_CLAMP, min(Z_CLAMP, z))
    return float(s.iloc[-1]), z, s.index[-1].date().isoformat()


def _change(s: pd.Series | None, periods: int) -> float | None:
    """Latest value minus the value `periods` observations ago."""
    if s is None:
        return None
    s = s.dropna()
    if len(s) <= periods:
        return None
    return float(s.iloc[-1] - s.iloc[-1 - periods])


def _classify_regime(series: dict[str, pd.Series]) -> tuple[str, dict[str, dict]]:
    """PLAN §4: 4 FRED dials vote risk-on/off; stress overrides on the vol
    term structure (<1 = backwardation) or fast HY-spread widening."""
    dials: dict[str, dict] = {}
    votes = 0
    counted = 0

    d_real = _change(series.get("DFII10"), 20)
    if d_real is not None:
        vote = 1 if d_real < 0 else -1
        votes += vote
        counted += 1
        dials["real_yields"] = {"label": "Real yields (DFII10, 20d)", "value": round(d_real, 3), "vote": vote}

    d_usd = _change(series.get("DTWEXBGS"), 20)
    if d_usd is not None:
        vote = 1 if d_usd < 0 else -1
        votes += vote
        counted += 1
        dials["usd"] = {"label": "Broad USD (DTWEXBGS, 20d)", "value": round(d_usd, 3), "vote": vote}

    hy = series.get("BAMLH0A0HYM2")
    hy_z = _zscore(hy) if hy is not None else None
    if hy_z is not None:
        vote = 1 if hy_z[1] < 0 else -1  # tighter than 2y mean = risk-on
        votes += vote
        counted += 1
        dials["credit"] = {"label": "HY OAS vs 2y mean", "value": round(hy_z[0], 3), "vote": vote}

    curve = series.get("T10Y2Y")
    if curve is not None and not curve.dropna().empty:
        level = float(curve.dropna().iloc[-1])
        vote = 1 if level > 0 else -1
        votes += vote
        counted += 1
        dials["curve"] = {"label": "2s10s level", "value": round(level, 3), "vote": vote}

    # Stress overrides (card #11: VIX3M/VIX < 1.0; fast HY widening).
    stress = False
    term = series.get("VIX_TERM")
    if term is not None and not term.dropna().empty and float(term.dropna().iloc[-1]) < 1.0:
        stress = True
        dials["vix_term_stress"] = {"label": "VIX3M/VIX < 1 (backwardation)", "value": round(float(term.dropna().iloc[-1]), 3), "vote": -1}
    d_hy = _change(hy, 20)
    if d_hy is not None and d_hy > 0.50:  # +50bp in ~a month
        stress = True
        dials["hy_widening_stress"] = {"label": "HY OAS +50bp/20d", "value": round(d_hy, 3), "vote": -1}

    if stress:
        return "stress", dials
    if counted == 0:
        return "unknown", dials
    if votes >= 2:
        return "risk-on", dials
    if votes <= -2:
        return "risk-off", dials
    return "neutral", dials


def compute_composite(duck: DuckStore) -> CompositeResult | None:
    """Returns None when not enough data has been ingested yet."""
    series = _load_series(duck)
    if not series:
        return None
    _derive(series)

    buckets: list[Bucket] = []
    for spec in BUCKETS:
        comps: list[Component] = []
        for sid, sign in spec["members"]:
            s = series.get(sid)
            zs = _zscore(s) if s is not None else None
            if zs is None:
                continue
            value, z, ts = zs
            comps.append(Component(series_id=sid, sign=sign, value=round(value, 4), z=round(sign * z, 4), ts=ts))
        z = sum(c.z for c in comps) / len(comps) if comps else None
        buckets.append(
            Bucket(
                key=spec["key"], label=spec["label"], weight=spec["weight"],
                weight_used=0.0, z=round(z, 4) if z is not None else None,
                contribution=0.0, components=comps,
            )
        )

    available = [b for b in buckets if b.z is not None]
    if not available:
        return None
    total_w = sum(b.weight for b in available)
    score = 0.0
    for b in available:
        b.weight_used = round(b.weight / total_w, 4)
        b.contribution = round(b.weight_used * b.z * SCALE, 2)
        score += b.contribution
    score = max(-100.0, min(100.0, round(score, 2)))

    regime, dials = _classify_regime(series)

    return CompositeResult(
        score=score,
        regime=regime,
        computed_at=datetime.now(timezone.utc).isoformat(),
        buckets=buckets,
        dials=dials,
    )
