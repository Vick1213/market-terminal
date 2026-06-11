"""Correlation Cookbook engine (PLAN §3f, §4) — pure blocking computation.

Everything is computed in-process from series already stored in DuckDB
(ts_macro + ts_price) — no external source on the critical path. Hygiene per
PLAN §4: correlations run on log-returns (diffs for rate-like series), all
legs aligned on a common business-day calendar with a bounded forward-fill.

Per corr card: rolling 30d + 90d Pearson corr, long-run mean±σ of the rolling
90d corr, z of the current corr, sign-flip test. BROKEN on sign-flip or
|z|>2, WATCH on |z|>1. Ratio/term/curve cards score the level instead.
Callers dispatch via ``run_in_executor`` (pandas + DuckDB reads).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.corr.cards import (
    CARDS, CARDS_BY_ID, HEATMAP_LEGS, MACRO_SERIES, MONTHLY_SERIES, PRICE_LEGS,
    CardSpec, Leg,
)
from app.db.duck import DuckStore

log = logging.getLogger("market.corr")

HISTORY_START = "2015-01-01"
FFILL_LIMIT = 10          # business days a value may carry forward (weekly FRED prints)
STALE_DAYS = 21           # newest common observation older than this -> no-data
BASELINE_MIN_OBS = 120    # rolling-corr history needed before the baseline is trusted
SIGN_DEAD_ZONE = 0.05     # |corr| below this never counts as a sign-flip
LAG_SCAN = 20             # ±days for the daily lead-lag scan
LAG_WINDOW = 252          # trailing window the lag scan correlates over
WEEKLY_LAG_MAX = 8        # net-liq leads by ~5-6 weeks; scan 0..8
WEEKLY_LAG_WINDOW = 104   # trailing weeks for the weekly lag scan
# Monthly series carry forward up to this many calendar days past their last
# print (one print + publication lag). Bounded so a dead DBnomics mirror
# still degrades to no-data via the normal staleness gate instead of posing
# as a current reading forever.
MONTHLY_EXTEND_DAYS = 45


def _f(x) -> float | None:
    """float or None — never NaN/inf into JSON."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _r(x, nd: int = 3) -> float | None:
    v = _f(x)
    return round(v, nd) if v is not None else None


def _epoch(ts: pd.Timestamp) -> int:
    return int(ts.replace(tzinfo=timezone.utc).timestamp())


# --- series loading -------------------------------------------------------


def load_series(duck: DuckStore) -> dict[str, pd.Series]:
    """All cookbook inputs as date-indexed series, keyed P:/M:/D: (cards.py)."""
    out: dict[str, pd.Series] = {}

    ph = ", ".join("?" for _ in MACRO_SERIES)
    df = duck.fetchdf(
        f"SELECT series_id, ts, value FROM ts_macro "
        f"WHERE series_id IN ({ph}) AND ts >= ? AND value IS NOT NULL ORDER BY ts",
        [*MACRO_SERIES, HISTORY_START],
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["ts"]).dt.normalize()
        for sid, g in df.groupby("series_id"):
            out[f"M:{sid}"] = g.groupby("date")["value"].last().sort_index()

    # Monthly prints -> daily step series with a bounded carry-forward, so
    # the weekly cards see full coverage between prints (the generic 10-bday
    # ffill in _align can't bridge a monthly gap).
    for sid in MONTHLY_SERIES:
        s = out.get(f"M:{sid}")
        if s is None or s.empty:
            continue
        end = min(
            pd.Timestamp.now().normalize(),
            s.index.max() + pd.Timedelta(days=MONTHLY_EXTEND_DAYS),
        )
        idx = pd.date_range(s.index.min(), end, freq="D")
        out[f"M:{sid}"] = s.reindex(idx).ffill()

    ph = ", ".join("?" for _ in PRICE_LEGS)
    df = duck.fetchdf(
        f"SELECT symbol, ts, close FROM ts_price "
        f"WHERE source = 'yahoo' AND symbol IN ({ph}) AND ts >= ? AND close IS NOT NULL "
        f"ORDER BY ts",
        [*PRICE_LEGS.keys(), HISTORY_START],
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["ts"]).dt.normalize()
        for sym, g in df.groupby("symbol"):
            out[f"P:{sym}"] = g.groupby("date")["close"].last().sort_index()

    _derive(out)
    return out


def _derive(series: dict[str, pd.Series]) -> None:
    def ratio(a: str, b: str) -> pd.Series | None:
        sa, sb = series.get(a), series.get(b)
        if sa is None or sb is None:
            return None
        idx = sa.index.union(sb.index)
        r = (sa.reindex(idx).ffill(limit=FFILL_LIMIT) / sb.reindex(idx).ffill(limit=FFILL_LIMIT)).dropna()
        return r if not r.empty else None

    cg = ratio("P:HG=F", "P:XAU")
    if cg is not None:
        series["D:COPPER_GOLD"] = cg
    gs = ratio("P:XAU", "P:XAG")
    if gs is not None:
        series["D:GOLD_SILVER"] = gs
    # VIX term structure: CBOE closes preferred, FRED VIXCLS fills the denominator.
    vix = series.get("M:VIX") if "M:VIX" in series else series.get("M:VIXCLS")
    if vix is not None and "M:VIX3M" in series:
        idx = series["M:VIX3M"].index.union(vix.index)
        term = (
            series["M:VIX3M"].reindex(idx).ffill(limit=FFILL_LIMIT)
            / vix.reindex(idx).ffill(limit=FFILL_LIMIT)
        ).dropna()
        if not term.empty:
            series["D:VIX_TERM"] = term
    walcl, rrp, tga = (series.get(k) for k in ("M:WALCL", "M:RRPONTSYD", "M:WTREGEN"))
    if walcl is not None and rrp is not None and tga is not None:
        # Weekly FRED proxy in $bn (WALCL/WTREGEN are $mn; RRPONTSYD is $bn).
        idx = walcl.index.union(rrp.index).union(tga.index)
        net = (
            walcl.reindex(idx).ffill() / 1000.0
            - rrp.reindex(idx).ffill()
            - tga.reindex(idx).ffill() / 1000.0
        ).dropna()
        # Splice the stored daily series (Phase-8 liquidity pipeline, $bn)
        # over the weekly proxy: daily resolution now, 2015+ history intact.
        daily = series.get("M:NET_LIQUIDITY")
        if daily is not None and not daily.dropna().empty:
            daily = daily.dropna()
            net = pd.concat([net[net.index < daily.index.min()], daily])
        if not net.empty:
            series["D:NET_LIQUIDITY"] = net


def _align(series: dict[str, pd.Series], keys: list[str]) -> pd.DataFrame | None:
    """Legs -> one business-day frame, weekend values rolled into Monday."""
    parts = {k: series[k] for k in keys if k in series}
    if len(parts) < len(keys):
        return None
    df = pd.DataFrame(parts)
    if df.empty:
        return None
    bdays = pd.bdate_range(df.index.min(), df.index.max())
    df = df.reindex(df.index.union(bdays)).ffill(limit=FFILL_LIMIT).reindex(bdays)
    return df.dropna(how="all")


def _returns(s: pd.Series, kind: str) -> pd.Series:
    if kind == "diff":
        return s.diff()
    return np.log(s.where(s > 0)).diff()


# --- results --------------------------------------------------------------


@dataclass
class CardResult:
    id: str
    num: int
    title: str
    mode: str
    status: str                      # holds | watch | broken | no-data
    legs: list[str]
    asof: str | None = None
    windows: list[str] = field(default_factory=lambda: ["30d", "90d"])
    corr30: float | None = None
    corr90: float | None = None
    baseline_mean: float | None = None
    baseline_std: float | None = None
    baseline_n: int = 0
    z: float | None = None           # z of corr (corr cards) or of the level
    sign_flip: bool = False
    value: float | None = None       # ratio / term / curve level
    value_label: str = ""
    lead_lag: dict | None = None
    secondary: dict | None = None
    notes: list[str] = field(default_factory=list)
    normal: str = ""
    rationale: str = ""
    breaks_when: str = ""


@dataclass
class CookbookResult:
    computed_at: str
    regime: str
    regime_score: float | None
    cards: list[CardResult]
    stress: dict
    heatmap: dict

    def to_json(self) -> str:
        return json.dumps(asdict(self))


# --- per-card computation --------------------------------------------------


def _stale(df: pd.DataFrame) -> bool:
    last = df.dropna().index.max()
    return last is None or (pd.Timestamp.now().normalize() - last).days > STALE_DAYS


def _no_data(spec: CardSpec, note: str) -> CardResult:
    return CardResult(
        id=spec.id, num=spec.num, title=spec.title, mode=spec.mode,
        status="no-data", legs=[l.label for l in spec.legs], notes=[note],
        normal=spec.normal, rationale=spec.rationale, breaks_when=spec.breaks_when,
    )


def _corr_status(
    corr_now: float | None, mean: float | None, std: float | None,
    n: int, expected_sign: int,
) -> tuple[str, float | None, bool]:
    """PLAN §4 BROKEN detector -> (status, z, sign_flip)."""
    if corr_now is None:
        return "no-data", None, False
    z = None
    if mean is not None and std is not None and std > 1e-6 and n >= BASELINE_MIN_OBS:
        z = (corr_now - mean) / std
    base_sign = expected_sign
    if mean is not None and abs(mean) >= SIGN_DEAD_ZONE and n >= BASELINE_MIN_OBS:
        base_sign = 1 if mean > 0 else -1
    flip = bool(base_sign and abs(corr_now) > SIGN_DEAD_ZONE and np.sign(corr_now) != base_sign)
    if flip or (z is not None and abs(z) > 2):
        return "broken", z, flip
    if z is not None and abs(z) > 1:
        return "watch", z, flip
    return "holds", z, flip


def _lag_profile(a: pd.Series, b: pd.Series, lags: range, window: int) -> list[tuple[int, float]]:
    """corr(a.shift(k), b) per lag k over the trailing window — k>0 = a leads."""
    out: list[tuple[int, float]] = []
    for k in lags:
        pair = pd.DataFrame({"a": a.shift(k), "b": b}).dropna().tail(window)
        if len(pair) < max(40, window // 4):
            continue
        c = _f(pair["a"].corr(pair["b"]))
        if c is not None:
            out.append((k, c))
    return out


def _rolling_corrs(
    ra: pd.Series, rb: pd.Series, weekly: bool = False
) -> tuple[pd.Series, pd.Series, list[str]]:
    """THE rolling-corr window pair (short, long, labels) — single definition
    so _quick_corr / _corr_card / compute_card_history can never drift."""
    if weekly:
        return (
            ra.rolling(13, min_periods=8).corr(rb),
            ra.rolling(26, min_periods=16).corr(rb),
            ["13w", "26w"],
        )
    return (
        ra.rolling(30, min_periods=20).corr(rb),
        ra.rolling(90, min_periods=60).corr(rb),
        ["30d", "90d"],
    )


def _quick_corr(series: dict[str, pd.Series], legs: tuple[Leg, Leg]) -> dict | None:
    df = _align(series, [l.key for l in legs])
    if df is None or _stale(df):
        return None
    ra = _returns(df[legs[0].key], legs[0].ret)
    rb = _returns(df[legs[1].key], legs[1].ret)
    c30, c90, _ = _rolling_corrs(ra, rb)
    c30, c90 = c30.dropna(), c90.dropna()
    if c90.empty:
        return None
    return {
        "label": f"{legs[0].label} vs {legs[1].label}",
        "corr30": _r(c30.iloc[-1]) if not c30.empty else None,
        "corr90": _r(c90.iloc[-1]),
    }


def _corr_card(spec: CardSpec, series: dict[str, pd.Series]) -> CardResult:
    la, lb = spec.legs[0], spec.legs[1]
    df = _align(series, [la.key, lb.key])
    if df is None:
        return _no_data(spec, "missing series — price legs ingest shortly after boot")
    if _stale(df):
        return _no_data(spec, "stale data — newest common observation too old")

    if spec.weekly:
        wk = df.resample("W-FRI").last()
        ra = _returns(wk[la.key], la.ret)
        rb = _returns(wk[lb.key], lb.ret)
        profile = _lag_profile(ra, rb, range(0, WEEKLY_LAG_MAX + 1), WEEKLY_LAG_WINDOW)
        if not profile:
            return _no_data(spec, "not enough weekly history yet")
        peak_lag, peak_corr = max(profile, key=lambda t: abs(t[1]))
        c_short, c_long, windows = _rolling_corrs(ra.shift(peak_lag), rb, weekly=True)
        c_short, c_long = c_short.dropna(), c_long.dropna()
        lead = {
            "unit": "w", "peak_lag": peak_lag, "peak_corr": _r(peak_corr),
            "leader": spec.leader, "leads_now": peak_lag > 0,
            "decayed": peak_lag == 0 or (peak_corr or 0) * spec.expected_sign < 0,
            "profile": [{"lag": k, "corr": _r(c)} for k, c in profile],
        }
    else:
        ra = _returns(df[la.key], la.ret)
        rb = _returns(df[lb.key], lb.ret)
        c_short, c_long, windows = _rolling_corrs(ra, rb)
        c_short, c_long = c_short.dropna(), c_long.dropna()
        lead = None
        if spec.lead_lag:
            profile = _lag_profile(ra, rb, range(-LAG_SCAN, LAG_SCAN + 1), LAG_WINDOW)
            if profile:
                peak_lag, peak_corr = max(profile, key=lambda t: abs(t[1]))
                # spec.leader is always leg A by construction (cards.py).
                lead = {
                    "unit": "d", "peak_lag": peak_lag, "peak_corr": _r(peak_corr),
                    "leader": spec.leader, "leads_now": peak_lag > 0,
                    "decayed": peak_lag <= 0 or abs(peak_corr or 0) < 0.1,
                    "profile": [{"lag": k, "corr": _r(c)} for k, c in profile],
                }

    if c_long.empty:
        return _no_data(spec, "not enough overlapping history yet")

    corr_now = _f(c_long.iloc[-1])
    mean, std, n = _f(c_long.mean()), _f(c_long.std()), len(c_long)
    status, z, flip = _corr_status(corr_now, mean, std, n, spec.expected_sign)

    notes: list[str] = []
    if "credit_divergence" in spec.extra_flags:
        notes += _credit_flags(series)

    secondary = _quick_corr(series, spec.secondary) if spec.secondary else None

    return CardResult(
        id=spec.id, num=spec.num, title=spec.title, mode=spec.mode, status=status,
        legs=[la.label, lb.label],
        asof=c_long.index[-1].date().isoformat(),
        windows=windows,
        corr30=_r(c_short.iloc[-1]) if not c_short.empty else None,
        corr90=_r(corr_now),
        baseline_mean=_r(mean), baseline_std=_r(std), baseline_n=n,
        z=_r(z, 2), sign_flip=flip,
        lead_lag=lead, secondary=secondary, notes=notes,
        normal=spec.normal, rationale=spec.rationale, breaks_when=spec.breaks_when,
    )


def _credit_flags(series: dict[str, pd.Series]) -> list[str]:
    """Card #5 sub-flags: hidden divergence + complacency (PLAN §4)."""
    notes: list[str] = []
    df = _align(series, ["M:BAMLH0A0HYM2", "P:^GSPC"])
    if df is None or len(df.dropna()) < 21:
        return notes
    df = df.dropna()
    hy_chg = float(df["M:BAMLH0A0HYM2"].iloc[-1] - df["M:BAMLH0A0HYM2"].iloc[-21])
    spx_chg = float(df["P:^GSPC"].iloc[-1] / df["P:^GSPC"].iloc[-21] - 1)
    if hy_chg > 0.10 and spx_chg > 0:
        notes.append(
            f"hidden divergence: HY OAS +{hy_chg * 100:.0f}bp/20d while SPX +{spx_chg * 100:.1f}%"
        )
    hy = df["M:BAMLH0A0HYM2"]
    if float(hy.iloc[-1]) <= float(hy.quantile(0.2)):
        notes.append(f"credit complacency: HY OAS {hy.iloc[-1]:.2f}% in the tightest quintile since 2015")
    return notes


def _level_z(s: pd.Series) -> tuple[float | None, float | None, float | None, int]:
    s = s.dropna()
    if len(s) < 60:
        return None, None, None, len(s)
    v, mean, std = float(s.iloc[-1]), float(s.mean()), float(s.std())
    z = (v - mean) / std if std > 1e-6 else None
    return v, mean, z, len(s)


def _ratio_card(spec: CardSpec, series: dict[str, pd.Series]) -> CardResult:
    s = series.get(spec.legs[0].key)
    if s is None or s.dropna().empty:
        return _no_data(spec, "missing series")
    v, mean, z, n = _level_z(s)
    if v is None:
        return _no_data(spec, "not enough history yet")
    status = "broken" if z is not None and abs(z) > 2 else "watch" if z is not None and abs(z) > 1 else "holds"
    notes = []
    if spec.id == "gold_silver" and v > 90:
        notes.append("ratio above 90:1 — historical stress zone")
    return CardResult(
        id=spec.id, num=spec.num, title=spec.title, mode=spec.mode, status=status,
        legs=[spec.legs[0].label], asof=s.dropna().index[-1].date().isoformat(),
        baseline_mean=_r(mean), baseline_n=n, z=_r(z, 2),
        value=_r(v, 2), value_label=f"{v:.1f}:1", notes=notes,
        normal=spec.normal, rationale=spec.rationale, breaks_when=spec.breaks_when,
    )


def _term_card(spec: CardSpec, series: dict[str, pd.Series]) -> CardResult:
    s = series.get(spec.legs[0].key)
    if s is None or s.dropna().empty:
        return _no_data(spec, "missing VIX/VIX3M history")
    v, mean, z, n = _level_z(s.tail(504))  # ~2y window for the z
    last = float(s.dropna().iloc[-1])
    status = "broken" if last < 1.0 else "watch" if last < 1.05 else "holds"
    notes = ["backwardation — early risk-off trigger"] if last < 1.0 else []
    return CardResult(
        id=spec.id, num=spec.num, title=spec.title, mode=spec.mode, status=status,
        legs=[spec.legs[0].label], asof=s.dropna().index[-1].date().isoformat(),
        baseline_mean=_r(mean), baseline_n=n, z=_r(z, 2),
        value=_r(last, 3), value_label=f"{last:.2f}", notes=notes,
        normal=spec.normal, rationale=spec.rationale, breaks_when=spec.breaks_when,
    )


def _curve_card(spec: CardSpec, series: dict[str, pd.Series]) -> CardResult:
    t2 = series.get("M:T10Y2Y")
    t3 = series.get("M:T10Y3M")
    if t2 is None or t2.dropna().empty:
        return _no_data(spec, "missing T10Y2Y")
    t2 = t2.dropna()
    level2 = float(t2.iloc[-1])
    level3 = float(t3.dropna().iloc[-1]) if t3 is not None and not t3.dropna().empty else None
    inverted_recently = bool((t2.tail(252) < 0).any())
    notes: list[str] = []
    if level2 < 0 and (level3 or 0) < 0:
        status = "broken"
        notes.append("curve inverted (2s10s and 3m10y)")
    elif level3 is not None and level3 < 0:
        status = "watch"
        notes.append("3m10y re-inverted while 2s10s steepens")
    elif inverted_recently and level2 > 0:
        status = "watch"
        notes.append("fresh un-inversion after a long inversion — late-cycle steepening")
    else:
        status = "holds"
    if len(t2) > 60:
        chg = level2 - float(t2.iloc[-61])
        notes.append(f"2s10s {'+' if chg >= 0 else ''}{chg * 100:.0f}bp over 60d")
    if level3 is not None:
        notes.append(f"3m10y at {level3 * 100:+.0f}bp")
    return CardResult(
        id=spec.id, num=spec.num, title=spec.title, mode=spec.mode, status=status,
        legs=[l.label for l in spec.legs], asof=t2.index[-1].date().isoformat(),
        value=_r(level2, 3), value_label=f"{level2 * 100:+.0f}bp", notes=notes,
        normal=spec.normal, rationale=spec.rationale, breaks_when=spec.breaks_when,
    )


# --- stress radar / heatmap / regime ---------------------------------------


def _chg(s: pd.Series | None, periods: int) -> float | None:
    if s is None:
        return None
    s = s.dropna()
    if len(s) <= periods:
        return None
    return float(s.iloc[-1] - s.iloc[-1 - periods])


def _stress_radar(series: dict[str, pd.Series]) -> dict:
    """Aug-2024 confluence: yen rally + VIX backwardation + gold/silver rising."""
    lights = []
    d_jpy = _chg(series.get("P:USDJPY=X"), 20)
    if d_jpy is not None:
        lights.append({
            "key": "usdjpy", "label": "USDJPY rolling over (20d)",
            "on": d_jpy < 0, "value": _r(d_jpy, 2),
        })
    term = series.get("D:VIX_TERM")
    if term is not None and not term.dropna().empty:
        v = float(term.dropna().iloc[-1])
        lights.append({
            "key": "vix_term", "label": "VIX3M/VIX < 1 (backwardation)",
            "on": v < 1.0, "value": _r(v, 3),
        })
    d_gs = _chg(series.get("D:GOLD_SILVER"), 20)
    if d_gs is not None:
        lights.append({
            "key": "gold_silver", "label": "Gold/Silver ratio rising (20d)",
            "on": d_gs > 0, "value": _r(d_gs, 2),
        })
    return {"on": bool(lights) and all(l["on"] for l in lights), "lights": lights}


def _heatmap(series: dict[str, pd.Series]) -> dict:
    cols: dict[str, pd.Series] = {}
    for label, key, ret in HEATMAP_LEGS:
        s = series.get(key)
        if s is None:
            continue
        cols[label] = _returns(s, ret)
    if len(cols) < 2:
        return {"labels": [], "matrix": []}
    df = pd.DataFrame(cols)
    bdays = pd.bdate_range(df.index.min(), df.index.max())
    df = df.reindex(df.index.union(bdays)).reindex(bdays).tail(90)
    df = df.loc[:, df.notna().sum() >= 60]
    if df.shape[1] < 2:
        return {"labels": [], "matrix": []}
    corr = df.corr()
    labels = list(corr.columns)
    matrix = [[_r(corr.iloc[i, j], 2) for j in range(len(labels))] for i in range(len(labels))]
    return {"labels": labels, "matrix": matrix}


def _regime(duck: DuckStore) -> tuple[str, float | None]:
    """The §4 regime classifier already runs in the macro composite — reuse it."""
    row = duck.fetchone(
        "SELECT score, regime FROM macro_composite ORDER BY ts DESC LIMIT 1"
    )
    if row is None:
        return "unknown", None
    return row[1], _f(row[0])


# --- entry points -----------------------------------------------------------


def compute_cookbook(duck: DuckStore) -> CookbookResult | None:
    series = load_series(duck)
    if not series:
        return None

    cards: list[CardResult] = []
    for spec in CARDS:
        try:
            if spec.mode == "corr":
                cards.append(_corr_card(spec, series))
            elif spec.mode == "ratio":
                cards.append(_ratio_card(spec, series))
            elif spec.mode == "term":
                cards.append(_term_card(spec, series))
            elif spec.mode == "curve":
                cards.append(_curve_card(spec, series))
        except Exception:
            log.exception("card %s failed", spec.id)
            cards.append(_no_data(spec, "computation error — see API logs"))

    regime, score = _regime(duck)
    return CookbookResult(
        computed_at=datetime.now(timezone.utc).isoformat(),
        regime=regime,
        regime_score=score,
        cards=cards,
        stress=_stress_radar(series),
        heatmap=_heatmap(series),
    )


def persist_cookbook(duck: DuckStore, result: CookbookResult) -> None:
    today = datetime(*date.today().timetuple()[:3])
    rows = [
        (c.id, today, c.status, c.corr30, c.corr90, c.z, c.value, json.dumps(asdict(c)))
        for c in result.cards
    ]
    rows.append(("_meta", today, result.regime, None, None, None, None, result.to_json()))
    duck.executemany(
        "INSERT OR REPLACE INTO corr_snapshots "
        "(card_id, ts, status, corr30, corr90, z, value, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    # Chartable daily series: rolling-90 corr per corr card, levels for the rest.
    macro_rows = []
    for c in result.cards:
        v = c.corr90 if c.mode == "corr" else c.value
        if v is not None:
            macro_rows.append((f"CORR_{c.id.upper()}", today, v, "computed"))
    duck.executemany(
        "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) VALUES (?, ?, ?, ?)",
        macro_rows,
    )


def compute_card_history(duck: DuckStore, card_id: str, days: int = 1095) -> dict | None:
    """Drill-down payload: rolling-corr history, rebased legs, lead-lag profile."""
    spec = CARDS_BY_ID.get(card_id)
    if spec is None:
        return None
    series = load_series(duck)
    cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days)

    if spec.mode == "corr":
        card = _corr_card(spec, series)
        la, lb = spec.legs[0], spec.legs[1]
        df = _align(series, [la.key, lb.key])
        history: list[dict] = []
        legs_rebased: list[dict] = []
        if df is not None:
            if spec.weekly:
                wk = df.resample("W-FRI").last()
                ra, rb = _returns(wk[la.key], la.ret), _returns(wk[lb.key], lb.ret)
                lag = (card.lead_lag or {}).get("peak_lag", 0)
                c30, c90, _ = _rolling_corrs(ra.shift(lag), rb, weekly=True)
            else:
                ra, rb = _returns(df[la.key], la.ret), _returns(df[lb.key], lb.ret)
                c30, c90, _ = _rolling_corrs(ra, rb)
            hist = pd.DataFrame({"c30": c30, "c90": c90}).loc[lambda d: d.index >= cutoff]
            for ts, row in hist.iterrows():
                if pd.isna(row["c30"]) and pd.isna(row["c90"]):
                    continue
                history.append({"t": _epoch(ts), "corr30": _r(row["c30"]), "corr90": _r(row["c90"])})
            year = df.loc[df.index >= pd.Timestamp.now().normalize() - pd.Timedelta(days=365)].dropna()
            if len(year) > 1:
                base = year.iloc[0]
                for ts, row in year.iterrows():
                    a0, b0 = float(base[la.key]), float(base[lb.key])
                    if a0 and b0:
                        legs_rebased.append({
                            "t": _epoch(ts),
                            "a": _r(float(row[la.key]) / a0 * 100, 2),
                            "b": _r(float(row[lb.key]) / b0 * 100, 2),
                        })
        return {"card": asdict(card), "history": history, "legs_rebased": legs_rebased,
                "leg_labels": [la.label, lb.label]}

    # ratio / term / curve: level history (+ companion series for the curve).
    if spec.mode == "curve":
        card = _curve_card(spec, series)
        out: list[dict] = []
        t2, t3 = series.get("M:T10Y2Y"), series.get("M:T10Y3M")
        if t2 is not None:
            df = pd.DataFrame({"a": t2, "b": t3}).loc[lambda d: d.index >= cutoff]
            for ts, row in df.iterrows():
                if pd.isna(row["a"]) and pd.isna(row["b"]):
                    continue
                out.append({"t": _epoch(ts), "a": _r(row["a"]), "b": _r(row["b"])})
        return {"card": asdict(card), "levels": out, "leg_labels": ["2s10s", "3m10y"]}

    card = _ratio_card(spec, series) if spec.mode == "ratio" else _term_card(spec, series)
    s = series.get(spec.legs[0].key)
    out = []
    if s is not None:
        for ts, v in s.dropna().loc[lambda x: x.index >= cutoff].items():
            out.append({"t": _epoch(ts), "v": _r(float(v))})
    return {"card": asdict(card), "levels": out, "leg_labels": [spec.legs[0].label]}
