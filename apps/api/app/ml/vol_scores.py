"""Per-name daily volatility scorer (PLAN §13.9 step 2, architecture §13.5).

Persists a per-symbol, per-horizon forward-vol forecast every day. **This
module does not change any trading behaviour** — nothing reads
``ml_vol_scores`` yet; it exists purely to start the persist-and-grade loop
§13.10 says the market-level overlay (``vol_overlay.py``) never got.

Two estimators, each used only where §13.0c/§13.3 measured it winning —
resist the urge to pick one:

  * **HAR-63** — OLS on log-GK vol at 5/21/63-day lookbacks — produces the
    **rank** columns (``rank``/``pctile``) at BOTH horizons. Best rank IC
    (0.5235 h=5 / 0.7027 h=21), beats naive_gk in all 6 walk-forward folds.
  * **plain HAR (1,5,22)** — produces the **level** column (``pred_vol``),
    but is only admissible for sizing at **h=21**. Best RMSE/QLIKE/R² at both
    horizons and its calibration slope never left [0.8, 1.2] in any fold —
    unlike HAR-63, whose h=5 slope hit 1.355 in the COVID fold (§13.3): a
    ~35% underprediction of near-term vol during exactly the kind of shock
    sizing exists to survive. So ``level_admissible`` is False at h=5 (still
    stored, so the §13.6 grader can watch its calibration) and True at h=21.
  * **naive_gk** (trailing 21d mean Garman-Klass vol, zero parameters) is the
    fallback whenever either OLS fit is degenerate or a symbol's history is
    too short — used independently for the level and the rank column.

The GBM evaluated in §13.0/§13.0b/§13.0c is deliberately NOT here: it passed
every statistical admissibility bar but was shelved on engineering economics
and PIT-macro revision risk (§13.3 ruling). Reviving it is future work, not
this module's job.

**Point-in-time fitting (PLAN §13.3/§13.4).** Every OLS fit below uses ONLY
history whose ``labels.forward_realized_vol`` target has already resolved as
of the scoring date — that function itself NaNs the last ``h`` rows because
their forward window hasn't happened yet, and ``dropna()`` removes exactly
those rows before fitting. This makes it mechanically impossible for a fit to
see data past the scoring date, not merely disciplined about it. The
prediction step then applies the fitted coefficients to the LAST row's
features — trailing lookbacks, knowable at the scoring date even though that
row's own target is the still-unresolved value we are forecasting.

**Units (PLAN §13.4).** ``labels.forward_realized_vol(..., log=True)``
returns log-of-DAILY vol, not annualised (``annualize_vol`` is a separate
helper this module never calls). Every OLS fit above happens in that log
space; predictions are converted to a daily-sigma level via ``exp()``
explicitly at the point they leave log space, commented at each boundary.

**Shrinkage (PLAN §13.4) — POOLED across the reference panel, not per name.**
OLS-on-log-vol minimises squared error in log space; exponentiating it is a
biased estimator of the level (Jensen's inequality). The plain-HAR level fit
is corrected by an OLS calibration regression ``realised = a + b·predicted``,
and the corrected ``σ̂ = a + b·σ_pred`` is what ``pred_vol`` reports.

That regression is fit **once per horizon per scoring run, pooled across
every reference-panel symbol's trailing in-sample (predicted, realised)
pairs** (``_pooled_calibration``) — matching how §13.0a/§13.4's measured
slope (1.098 h=21 / 1.187 h=5, R² ~0.5) was actually produced: POOLED across
the whole panel, n ≈ 664,000. An earlier version of this module fit that
regression per symbol on ≤252 rows of *overlapping* h-day labels; the
effective independent sample there is only ``window/h`` (≈12 at h=21), so the
per-symbol slope was noise — it ranged from -0.31 to 0.77 across five
megacaps, and a negative slope silently INVERTS the forecast (more predicted
vol -> a lower emitted level). Pooling across ~140+ names multiplies the
effective sample by the panel width (window/h × n_symbols — thousands even at
h=21), which is what makes a 2-parameter OLS trustworthy here.

Every row's ``calib_a``/``calib_b`` at a given horizon is therefore the SAME
panel-level pair (unless that symbol itself fell back to naive_gk, which
always reports the identity map (0, 1) regardless of the pool). A sanity band
(``_CALIB_BAND``) and an explicit ``b <= 0`` guard mean a degenerate or noisy
pool can never reach a consumer: it falls back to the identity map — raw,
uncorrected HAR — and logs a WARNING, never a silently-wrong number. The
§13.6 grader watches this same pooled slope drift out of band on live data,
without refitting anything.

**Reference panel (PLAN §13.5).** Ranks/percentiles are always computed
against the FIXED 147-name research universe (``app.ml.universe.UNIVERSE``),
never against whatever set was requested — otherwise adding one watchlist
name would silently reshuffle every other name's rank. A requested symbol
outside that panel (crypto, metals, a new watchlist name) still gets scored
and ranked against it, but ``in_reference_panel=False`` flags that it isn't
an apples-to-apples comparison (§13.8: "do not silently rank them").

Run:  cd apps/api && .venv/bin/python -m app.ml.vol_scores --dry-run
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import duckdb
import numpy as np
import pandas as pd

from app.ml import labels
from app.ml.vol_baselines import _gk_daily_vol

log_name = "ml.vol_scores"

try:  # the 147-name research universe -- canonical list lives in universe.py
    from app.ml.universe import UNIVERSE as REFERENCE_PANEL
    from app.ml.universe import universe_db_path as _default_uni_db_path
except Exception:  # pragma: no cover - defensive: universe.py is another
    # in-flight deliverable this module depends on (PLAN §13.9 step 1); if it
    # is ever unavailable, fall back to the same literal list rather than
    # hard-failing every import of this module.
    REFERENCE_PANEL = tuple(sorted({
        "AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "AEP", "AMAT", "AMD", "AMGN", "AMT", "AMZN",
        "ANET", "APD", "AVGO", "AXP", "BA", "BAC", "BKNG", "BLK", "BMY", "BRK-B", "C", "CAT", "CB",
        "CCI", "CDNS", "CHTR", "CL", "CMCSA", "CMG", "COF", "COP", "COST", "CRM", "CSCO", "CSX",
        "CVS", "CVX", "D", "DD", "DE", "DHR", "DIS", "DOW", "DUK", "EA", "ECL", "ELV", "EMR", "EOG",
        "EQIX", "ETN", "EXC", "F", "FCX", "FDX", "GD", "GE", "GILD", "GIS", "GM", "GOOGL", "GS",
        "HD", "HON", "IBM", "INTC", "INTU", "ISRG", "JNJ", "JPM", "KHC", "KLAC", "KMB", "KMI", "KO",
        "LIN", "LLY", "LMT", "LOW", "LRCX", "MAR", "MCD", "MDLZ", "MDT", "META", "MMM", "MO", "MPC",
        "MRK", "MS", "MSFT", "MU", "NEE", "NEM", "NFLX", "NKE", "NOW", "NSC", "NUE", "NVDA", "O",
        "ORCL", "ORLY", "OXY", "PANW", "PEP", "PFE", "PG", "PGR", "PLD", "PM", "PNC", "PSA", "PSX",
        "QCOM", "RTX", "SBUX", "SCHW", "SHW", "SLB", "SNPS", "SO", "SPG", "SPGI", "SRE", "T", "TGT",
        "TJX", "TMO", "TMUS", "TSLA", "TXN", "UNH", "UNP", "UPS", "USB", "VLO", "VRTX", "VZ", "WELL",
        "WFC", "WMB", "WMT", "XEL", "XOM",
    }))

    def _default_uni_db_path() -> str:  # type: ignore[misc]
        repo = Path(__file__).resolve().parents[4]
        return str(repo / "data" / "ml" / "universe.duckdb")

REFERENCE_PANEL = tuple(REFERENCE_PANEL)

DEFAULT_HORIZONS = (5, 21)
LEVEL_ADMISSIBLE_HORIZON = 21          # PLAN §13.3/§13.4: only h=21 levels may size

_HAR_LOOKBACKS = (1, 5, 22)            # plain HAR -> LEVEL column
_HAR63_LOOKBACKS = (5, 21, 63)         # HAR-63 -> RANK columns
_NAIVE_WIN = 21                        # naive_gk fallback window (vol_baselines convention)

# Judgment calls (PLAN §13 gives no exact numbers for these constants):
_MIN_OHLC_ROWS = 30                    # below this even naive_gk is too noisy to trust;
                                        # skip and report rather than emit a number.

# --- Per-symbol PIT fit guard: horizon- and parameter-aware, not a flat count
# A flat row-count floor (this module originally used vol_baselines._har_ic's
# per-fold `tr.sum() < 200` convention) ignores that forward h-day vol labels
# OVERLAP: consecutive rows share h-1 days of their forward window, so a fit's
# EFFECTIVE independent sample is only ~n_rows/h, not n_rows. 200 rows at
# h=21 is only ~10 effective observations for a 4-parameter regression
# (intercept + 3 lookback terms) -- interpolation, not a fit. A parallel
# experiment demonstrated exactly this failure mode concretely: a HAR-family
# fit admitted at n_fit=2,448 rows produced wildly degenerate predictions
# (QLIKE 8.8-15.8 vs a normal ~0.74). See `_min_fit_obs`.
_MIN_EFFECTIVE_OBS_PER_PARAM = 40      # target ratio of effective (non-overlapping)
                                        # observations per fitted OLS parameter. The
                                        # usual regression rule of thumb is ~10-20
                                        # obs/parameter; 40 is a deliberately
                                        # conservative multiple of that given how thin
                                        # the *effective* (label-deduplicated) sample
                                        # already is here, and given the concrete
                                        # QLIKE-blowup counter-example above.
_MIN_FIT_OBS_FLOOR = 504               # absolute floor (~2 trading years) regardless
                                        # of horizon/parameter count, so a tiny (h,
                                        # n_params) combination can't trivially clear
                                        # the ratio-based minimum with a handful of days.

# --- Pooled shrinkage calibration (PLAN §13.4 fix) --------------------------
# Fit ONCE per horizon per scoring run, pooled across the reference panel --
# see the module docstring's "Shrinkage" section for why a per-symbol fit was
# wrong (noise from too few effective independent observations).
_CALIB_WINDOW = 756                    # >=3 trailing years per contributing symbol.
                                        # Labels overlap over the h-day forward window,
                                        # so a single name's effective independent count
                                        # is only ~window/h (≈36 at h=21); pooling across
                                        # _CALIB_MIN_SYMBOLS+ names multiplies that by the
                                        # panel width, giving thousands of effective
                                        # observations even at h=21 -- comfortably enough
                                        # to trust a 2-parameter OLS.
_CALIB_MIN_SYMBOLS = 20                # minimum reference-panel symbols with a valid
                                        # HAR fit contributing to the pool; below this the
                                        # pool itself is too thin to trust.
_CALIB_MIN_POOLED_OBS = 500            # minimum total pooled (predicted, realised) pairs.
_CALIB_BAND = (0.5, 2.0)               # sanity band on the pooled slope b; outside this
                                        # (or b<=0, checked separately and unconditionally)
                                        # -> identity fallback (0.0, 1.0), logged as a
                                        # WARNING, never silently applied.

_OHLC_SQL = (
    "SELECT ts, open, high, low, close FROM ts_price WHERE symbol=? AND source='yahoo' "
    "AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL "
    "AND low>0 AND open>0 ORDER BY ts"
)

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS ml_vol_scores (
    ts                  TIMESTAMP NOT NULL,
    symbol              VARCHAR NOT NULL,
    horizon             INTEGER NOT NULL,
    estimator           VARCHAR NOT NULL,   -- 'har' | 'naive_gk' -- producer of pred_vol
    pred_vol            DOUBLE,             -- forecast daily sigma, shrinkage-calibrated
    level_admissible    BOOLEAN NOT NULL,   -- True only at h=21 (PLAN §13.3/§13.4)
    calib_a             DOUBLE,             -- PANEL-LEVEL shrinkage intercept (pooled OLS
                                             -- across the reference panel, once per horizon
                                             -- per run -- NOT fit per symbol): realised =
                                             -- a + b*predicted. Identity (0,1) on a naive_gk
                                             -- fallback row.
    calib_b             DOUBLE,             -- PANEL-LEVEL shrinkage slope, same pooled fit
                                             -- as calib_a -- §13.6 grader trips if it drifts
                                             -- out of [0.8, 1.2]
    rank                INTEGER,            -- HAR-63 rank vs the fixed reference panel
    pctile              DOUBLE,             -- HAR-63 percentile vs the fixed reference panel, 0..1
    in_reference_panel  BOOLEAN NOT NULL,   -- False = ranked against the panel but not a member
    n_obs               INTEGER,            -- rows used in the level (HAR) PIT fit, or GK obs
                                             -- backing naive_gk when that fallback fired
    created_at          TIMESTAMP NOT NULL,
    PRIMARY KEY (ts, symbol, horizon)
);
"""


def ensure_table(duck) -> None:
    """Idempotent create -- also declared in db/schema.py::init_duckdb (the
    convention every other main-DB table follows) so it exists on normal app
    boot; kept here too, DDL-identical, so a bare/temp DuckStore (tests, the
    standalone CLI) works without running the full schema bootstrap."""
    duck.execute(_TABLE_DDL)


# --- OHLC loading / routing --------------------------------------------------


class _RawReader:
    """Wraps a raw ``duckdb.Connection`` in the same ``fetchall(sql, params)``
    surface ``DuckStore`` exposes, so ``_load_ohlc_routed`` can treat a
    read-only CLI connection and the live API's shared ``DuckStore``
    uniformly."""

    def __init__(self, con) -> None:
        self._con = con

    def fetchall(self, sql: str, params=None):
        return self._con.execute(sql, params or []).fetchall()


def _load_ohlc_routed(symbol: str, uni_reader, main_reader) -> pd.DataFrame | None:
    """Universe-first, main-fallback OHLC routing (PLAN §13 deliverable 3).

    Mirrors ``cross_section.RoutingDuck``'s ts_price fallback (try the
    universe DB, an empty result falls back to main) rather than
    instantiating that class directly: ``RoutingDuck`` expects a raw
    ``duckdb.Connection`` for BOTH stores, but the live job must route
    ``market.duckdb`` reads through the shared ``DuckStore`` instead of ever
    opening a second connection to it in-process (``db/duck.py``). Using a
    uniform ``fetchall(sql, params)`` reader on both sides lets one function
    serve the CLI (two raw read-only connections) and the scheduler job
    (a fresh read-only universe connection + the shared ``DuckStore``) alike.
    """
    rows = uni_reader.fetchall(_OHLC_SQL, [symbol]) if uni_reader is not None else []
    if not rows and main_reader is not None:
        rows = main_reader.fetchall(_OHLC_SQL, [symbol])
    if not rows:
        return None
    idx = pd.to_datetime([r[0] for r in rows]).normalize()
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c"], index=idx).drop(columns="ts")
    return df.astype(float)


# --- estimator core: HAR feature build, PIT fit, shrinkage -------------------


def _log_gk_feats(gk: pd.Series, lookbacks: tuple[int, ...]) -> pd.DataFrame:
    """log-GK-vol lookback features, mirroring vol_baselines._build's
    lrv_d/lrv_w/lrv_m construction: lookback==1 is the raw daily log-GK value
    (no rolling); lookback>1 is the log of the trailing rolling MEAN of GK
    vol over that window."""
    out = {}
    for w in lookbacks:
        if w == 1:
            out[f"lrv_{w}"] = np.log(gk.clip(lower=1e-6))
        else:
            out[f"lrv_{w}"] = np.log(
                gk.rolling(w, min_periods=max(3, w // 2)).mean().clip(lower=1e-6)
            )
    return pd.DataFrame(out, index=gk.index)


def _min_fit_obs(lookbacks: tuple[int, ...], h: int) -> int:
    """Horizon- and parameter-aware minimum PIT fit sample -- see the
    ``_MIN_EFFECTIVE_OBS_PER_PARAM``/``_MIN_FIT_OBS_FLOOR`` comments above for
    the reasoning. ``n_params`` is derived from the lookback spec (``len(
    lookbacks) + 1`` for the OLS intercept) rather than hardcoded, so HAR
    (3 lookbacks -> 4 params) and HAR-63 (also 3 -> 4 params today, but this
    stays correct if that ever changes) both scale automatically.

    Concretely, with the 3-lookback HAR/HAR-63 specs (n_params=4): h=5 needs
    >= 800 rows, h=21 needs >= 3,360 rows (~13.3 years). A recent-IPO symbol
    that doesn't clear that at h=21 legitimately falls back to naive_gk there
    rather than emitting a thin, unstable HAR level.
    """
    n_params = len(lookbacks) + 1
    return max(_MIN_FIT_OBS_FLOOR, _MIN_EFFECTIVE_OBS_PER_PARAM * n_params * h)


def _fit_har_pit(
    gk: pd.Series, close: pd.Series, h: int, lookbacks: tuple[int, ...]
) -> dict | None:
    """Point-in-time HAR OLS (adapts vol_baselines._har_ic's lstsq fitting to
    a single live fit instead of walk-forward CV): fit on all history whose
    h-day-forward target has already resolved as of the last available row,
    then predict that last row. See the module docstring's "Point-in-time
    fitting" section for why this cannot see future data. Returns None if the
    fit is degenerate (too few rows per ``_min_fit_obs``, singular design,
    non-finite result) so the caller can fall back to naive_gk."""
    feats = _log_gk_feats(gk, lookbacks)
    cols = list(feats.columns)
    target = labels.forward_realized_vol(close, h, log=True)  # log-of-daily-vol
    df = feats.copy()
    df["_target"] = target.reindex(feats.index)
    fit_rows = df.dropna()  # PIT boundary: drops warm-up AND the unresolved tail
    if len(fit_rows) < _min_fit_obs(lookbacks, h):
        return None

    X = fit_rows[cols].to_numpy(float)
    y = fit_rows["_target"].to_numpy(float)
    Xd = np.column_stack([np.ones(len(X)), X])
    try:
        beta, _resid, mat_rank, _sv = np.linalg.lstsq(Xd, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if mat_rank < Xd.shape[1] or not np.all(np.isfinite(beta)):
        return None  # singular / degenerate design

    last_feat = feats.iloc[-1][cols]
    if last_feat.isna().any():
        return None  # not enough trailing history at the scoring date itself
    x_last = np.r_[1.0, last_feat.to_numpy(float)]
    pred_log = float(x_last @ beta)  # still LOG space -- exp() at the caller boundary
    if not np.isfinite(pred_log):
        return None

    fitted_log = Xd @ beta
    return {
        "pred_log": pred_log,
        "fitted_log": fitted_log,
        "actual_log": y,
        "n_obs": int(len(fit_rows)),
    }


def _pooled_calibration(
    fits_by_symbol: dict[str, dict[int, dict]], h: int, window: int
) -> tuple[float, float, dict]:
    """PLAN §13.4 fix: the shrinkage calibration ``realised = a + b*predicted``
    fit ONCE, POOLED across every reference-panel symbol's trailing ``window``
    in-sample (predicted, realised) LEVEL pairs from its own plain-HAR fit --
    NOT fit per symbol (see the module docstring's "Shrinkage" section for why
    a per-symbol fit on overlapping labels is noise). ``fits_by_symbol`` maps
    symbol -> ``{h: raw_fit_dict}`` as produced by ``_raw_fits``.

    Returns ``(a, b, meta)``. Falls back to the identity map ``(0.0, 1.0)`` --
    raw, uncorrected HAR -- whenever: too few contributing symbols or pooled
    observations, a singular/degenerate fit, a non-finite intercept, ``b<=0``
    (checked FIRST and unconditionally -- a non-positive slope must NEVER be
    applied, it would flatten or invert the forecast), or ``b`` outside
    ``_CALIB_BAND``. ``meta['fallback_reason']`` is set (else ``None``) so the
    caller can log a WARNING with the reason and pool size.
    """
    pred_chunks: list[np.ndarray] = []
    real_chunks: list[np.ndarray] = []
    n_symbols = 0
    for per_h in fits_by_symbol.values():
        fit = (per_h.get(h) or {}).get("har_fit")
        if fit is None:
            continue
        pred_level = np.exp(fit["fitted_log"])   # LOG -> LEVEL boundary
        real_level = np.exp(fit["actual_log"])   # LOG -> LEVEL boundary
        if len(pred_level) > window:
            pred_level = pred_level[-window:]
            real_level = real_level[-window:]
        pred_chunks.append(pred_level)
        real_chunks.append(real_level)
        n_symbols += 1

    meta: dict = {"n_symbols": n_symbols, "n_obs": 0, "raw_b": None, "fallback_reason": None}
    IDENTITY = (0.0, 1.0)
    if n_symbols < _CALIB_MIN_SYMBOLS:
        meta["fallback_reason"] = f"only {n_symbols} contributing symbols (< {_CALIB_MIN_SYMBOLS})"
        return (*IDENTITY, meta)

    pred_all = np.concatenate(pred_chunks)
    real_all = np.concatenate(real_chunks)
    meta["n_obs"] = int(len(pred_all))
    if len(pred_all) < _CALIB_MIN_POOLED_OBS or np.nanstd(pred_all) < 1e-12:
        meta["fallback_reason"] = f"only {len(pred_all)} pooled obs (< {_CALIB_MIN_POOLED_OBS})"
        return (*IDENTITY, meta)

    Xc = np.column_stack([np.ones(len(pred_all)), pred_all])
    try:
        beta, _resid, mat_rank, _sv = np.linalg.lstsq(Xc, real_all, rcond=None)
    except np.linalg.LinAlgError:
        meta["fallback_reason"] = "singular pooled design"
        return (*IDENTITY, meta)
    if mat_rank < 2 or not np.all(np.isfinite(beta)):
        meta["fallback_reason"] = "degenerate pooled fit"
        return (*IDENTITY, meta)

    a, b = float(beta[0]), float(beta[1])
    meta["raw_a"], meta["raw_b"] = a, b
    if not np.isfinite(a):
        meta["fallback_reason"] = "non-finite pooled intercept"
        return (*IDENTITY, meta)
    if b <= 0:  # NEVER applied, under any circumstance -- would flatten/invert the forecast
        meta["fallback_reason"] = f"non-positive pooled slope b={b:.4f}"
        return (*IDENTITY, meta)
    lo, hi = _CALIB_BAND
    if not (lo <= b <= hi):
        meta["fallback_reason"] = f"pooled slope b={b:.4f} outside band {_CALIB_BAND}"
        return (*IDENTITY, meta)

    return a, b, meta


def _raw_fits(df: pd.DataFrame, horizons: Sequence[int]) -> dict[int, dict]:
    """Per-symbol raw fits at each horizon -- NO calibration applied here
    (that happens once, pooled, in ``score_universe``). Returns ``{h: {...}}``
    always (never ``None``); a horizon with ``naive_last is None`` and
    ``har_fit is None`` is simply unscoreable and ``_finalize`` will skip it.
    """
    gk = _gk_daily_vol(df["o"], df["h"], df["l"], df["c"])
    naive = gk.rolling(_NAIVE_WIN, min_periods=max(5, _NAIVE_WIN // 2)).mean()  # daily-sigma level, no log
    close = df["c"]
    n_gk_obs = int(gk.dropna().shape[0])
    ts = df.index[-1]

    out: dict[int, dict] = {}
    for h in horizons:
        naive_last = float(naive.iloc[-1]) if len(naive) else float("nan")
        if not np.isfinite(naive_last):
            out[h] = {"ts": ts, "naive_last": None, "n_gk_obs": n_gk_obs, "har_fit": None, "har63_fit": None}
            continue
        # --- LEVEL fit: plain HAR(1,5,22) -- admissible for sizing at h=21 only ---
        har_fit = _fit_har_pit(gk, close, h, _HAR_LOOKBACKS)
        # --- RANK fit: HAR-63(5,21,63) -- independent of the level fit ------
        har63_fit = _fit_har_pit(gk, close, h, _HAR63_LOOKBACKS)
        out[h] = {
            "ts": ts, "naive_last": naive_last, "n_gk_obs": n_gk_obs,
            "har_fit": har_fit, "har63_fit": har63_fit,
        }
    return out


def _finalize(symbol_fit: dict, h: int, calib_a: float, calib_b: float) -> dict | None:
    """Combine one symbol's raw per-horizon fit with the horizon's POOLED
    calibration into the final level/rank fields for one output row. Returns
    ``None`` when nothing -- not even naive_gk -- can be emitted."""
    har_fit = symbol_fit["har_fit"]
    naive_last = symbol_fit["naive_last"]

    estimator = pred_vol = n_obs = None
    if har_fit is not None:
        raw_level = float(np.exp(har_fit["pred_log"]))  # LOG -> LEVEL boundary
        level = calib_a + calib_b * raw_level
        if np.isfinite(level) and level > 0:
            estimator, pred_vol, n_obs = "har", level, har_fit["n_obs"]
        else:
            har_fit = None  # blew up despite a validated calibration -- fall through

    if har_fit is None:
        if naive_last is None:
            return None
        estimator, pred_vol, n_obs = "naive_gk", naive_last, symbol_fit["n_gk_obs"]
        calib_a, calib_b = 0.0, 1.0  # identity for the fallback row itself

    har63_fit = symbol_fit["har63_fit"]
    rank_basis = None
    if har63_fit is not None:
        rank_basis = float(np.exp(har63_fit["pred_log"]))  # LOG -> LEVEL boundary
        if not (np.isfinite(rank_basis) and rank_basis > 0):
            rank_basis = None
    if rank_basis is None:
        rank_basis = naive_last
    if rank_basis is None:
        return None  # no naive fallback available either -- truly unscoreable

    return {
        "ts": symbol_fit["ts"],
        "estimator": estimator,
        "pred_vol": pred_vol,
        "level_admissible": bool(h == LEVEL_ADMISSIBLE_HORIZON),
        "calib_a": calib_a,
        "calib_b": calib_b,
        "n_obs": n_obs,
        "_rank_basis": rank_basis,
    }


# --- public API ---------------------------------------------------------------

_OUTPUT_COLS = [
    "ts", "symbol", "horizon", "estimator", "pred_vol", "level_admissible",
    "calib_a", "calib_b", "rank", "pctile", "in_reference_panel", "n_obs",
]


def score_universe(
    symbols: Sequence[str],
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    *,
    uni_reader=None,
    main_reader=None,
    reference_panel: Sequence[str] | None = None,
    as_of: str | pd.Timestamp | None = None,
    calib_window: int = _CALIB_WINDOW,
    skip_report: list[dict] | None = None,
) -> pd.DataFrame:
    """Score ``symbols`` at each of ``horizons``, one row per (symbol,
    horizon), columns per the module docstring / ``_OUTPUT_COLS``.

    ``uni_reader``/``main_reader`` are anything exposing ``fetchall(sql,
    params)`` (a wrapped read-only connection, or the live ``DuckStore``) --
    see ``_load_ohlc_routed``. ``reference_panel`` defaults to
    ``REFERENCE_PANEL`` (the 147-name research universe); ranks/percentiles
    are ALWAYS computed against it, regardless of what ``symbols`` asks for
    (§13.5 — a fixed panel, not the scored set). ``as_of`` truncates each
    symbol's OHLC to rows on or before that date before fitting -- mainly a
    testing/backtest hook to prove the PIT boundary; live callers normally
    leave it ``None`` (score off each symbol's latest available bar).
    ``skip_report``, if passed a list, is appended with one dict per
    symbol/horizon that could not be scored (no clean OHLC, or history too
    short even for the naive_gk fallback) and why.
    """
    ref_panel = tuple(reference_panel) if reference_panel is not None else REFERENCE_PANEL
    ref_set = set(ref_panel)
    horizons = tuple(int(h) for h in horizons)
    requested = list(dict.fromkeys(symbols))  # de-dup, preserve order
    # Reference-panel fits must be computed even for symbols the caller didn't
    # request scores for -- both for the rank distribution AND (only for
    # panel members) the pooled calibration below -- but only requested
    # symbols get output rows.
    universe_syms = list(dict.fromkeys(requested + list(ref_panel)))

    # --- phase 1: raw per-symbol fits, no calibration applied yet ----------
    raw: dict[str, dict[int, dict]] = {}
    for sym in universe_syms:
        ohlc = _load_ohlc_routed(sym, uni_reader, main_reader)
        if ohlc is None:
            if skip_report is not None:
                skip_report.append({"symbol": sym, "reason": "no_clean_ohlc"})
            raw[sym] = {h: {"ts": None, "naive_last": None, "n_gk_obs": 0,
                             "har_fit": None, "har63_fit": None} for h in horizons}
            continue
        frame = ohlc.loc[: pd.Timestamp(as_of)] if as_of is not None else ohlc
        if len(frame) < _MIN_OHLC_ROWS:
            if skip_report is not None:
                skip_report.append({
                    "symbol": sym, "reason": "insufficient_history", "rows": len(frame),
                })
            raw[sym] = {h: {"ts": None, "naive_last": None, "n_gk_obs": 0,
                             "har_fit": None, "har63_fit": None} for h in horizons}
            continue
        raw[sym] = _raw_fits(frame, horizons)

    # --- phase 2: pooled shrinkage calibration, once per horizon, over the
    #     FIXED reference panel only (PLAN §13.4 fix -- see module docstring).
    calib_by_h: dict[int, tuple[float, float]] = {}
    ref_raw = {s: raw[s] for s in ref_panel if s in raw}
    for h in horizons:
        a, b, meta = _pooled_calibration(ref_raw, h, calib_window)
        calib_by_h[h] = (a, b)
        if meta["fallback_reason"]:
            logging.getLogger(log_name).warning(
                "vol_scores: pooled calibration h=%s -> identity fallback (%s); "
                "contributing symbols=%s pooled n_obs=%s",
                h, meta["fallback_reason"], meta["n_symbols"], meta["n_obs"],
            )

    # --- phase 3: finalize every (symbol, horizon) using the pooled calib --
    finalized: dict[str, dict[int, dict | None]] = {}
    for sym in universe_syms:
        finalized[sym] = {}
        for h in horizons:
            a, b = calib_by_h[h]
            rec = _finalize(raw[sym][h], h, a, b)
            finalized[sym][h] = rec
            if rec is None and skip_report is not None and sym in requested:
                skip_report.append({
                    "symbol": sym, "horizon": h,
                    "reason": "insufficient_history_for_naive_gk",
                })

    # Fixed reference-panel distribution per horizon (level units).
    ref_dist: dict[int, np.ndarray] = {}
    for h in horizons:
        vals = [
            finalized[s][h]["_rank_basis"] for s in ref_panel
            if finalized.get(s, {}).get(h) is not None
        ]
        ref_dist[h] = np.asarray(vals, dtype=float)

    rows = []
    for sym in requested:
        for h in horizons:
            rec = finalized.get(sym, {}).get(h)
            if rec is None:
                continue
            arr = ref_dist[h]
            if arr.size:
                rnk = int((arr <= rec["_rank_basis"]).sum())
                pctile = round(rnk / arr.size, 4)
            else:
                rnk, pctile = None, None
            rows.append({
                "ts": rec["ts"], "symbol": sym, "horizon": h, "estimator": rec["estimator"],
                "pred_vol": rec["pred_vol"], "level_admissible": rec["level_admissible"],
                "calib_a": rec["calib_a"], "calib_b": rec["calib_b"],
                "rank": rnk, "pctile": pctile,
                "in_reference_panel": sym in ref_set, "n_obs": rec["n_obs"],
            })
    return pd.DataFrame(rows, columns=_OUTPUT_COLS)


def write_scores(duck, df: pd.DataFrame) -> int:
    """Idempotent upsert into ``ml_vol_scores``, keyed on ``(ts, symbol,
    horizon)`` -- a re-run replaces rather than duplicates (PLAN §13.5
    deliverable 2). Writes through the shared ``DuckStore`` only."""
    if df.empty:
        return 0
    ensure_table(duck)
    now = datetime.now(timezone.utc)

    def _n(v, cast):
        return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else cast(v)

    rows = [
        (
            pd.Timestamp(r.ts).to_pydatetime(), r.symbol, int(r.horizon), r.estimator,
            _n(r.pred_vol, float), bool(r.level_admissible),
            _n(r.calib_a, float), _n(r.calib_b, float),
            _n(r.rank, int), _n(r.pctile, float),
            bool(r.in_reference_panel), _n(r.n_obs, int),
            now,
        )
        for r in df.itertuples(index=False)
    ]
    duck.executemany(
        "INSERT OR REPLACE INTO ml_vol_scores "
        "(ts, symbol, horizon, estimator, pred_vol, level_admissible, calib_a, calib_b, "
        " rank, pctile, in_reference_panel, n_obs, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


# --- scheduler entrypoint (default-OFF; wiring left to the caller) ----------


def _open_universe_reader(uni_db: str | Path | None):
    """Fresh read-only connection to universe.duckdb -- a SEPARATE file from
    market.duckdb, so opening it in-process never conflicts with the shared
    DuckStore's writer handle. Returns (reader, raw_connection_to_close)."""
    path = Path(uni_db) if uni_db else Path(_default_uni_db_path())
    if not path.exists():
        return None, None
    con = duckdb.connect(str(path), read_only=True)
    return _RawReader(con), con


def _score_and_persist(
    duck, horizons: Sequence[int] = DEFAULT_HORIZONS, symbols: Sequence[str] | None = None,
    uni_db: str | None = None, calib_window: int = _CALIB_WINDOW,
) -> dict:
    """Blocking body of run_vol_scores_job -- offloaded via run_in_executor.
    ``duck`` (the shared DuckStore) is the ONLY handle used against
    market.duckdb; the universe DB gets its own short-lived read-only
    connection, closed before returning."""
    uni_reader, uni_con = _open_universe_reader(uni_db)
    try:
        syms = list(symbols) if symbols else list(REFERENCE_PANEL)
        skips: list[dict] = []
        df = score_universe(
            syms, horizons, uni_reader=uni_reader, main_reader=duck,
            calib_window=calib_window, skip_report=skips,
        )
        n = write_scores(duck, df)
    finally:
        if uni_con is not None:
            uni_con.close()
    as_of = None
    if not df.empty:
        as_of = str(pd.Timestamp(df["ts"].max()).date())
    return {"rows": n, "symbols": len(syms), "skipped": len(skips), "as_of": as_of}


async def run_vol_scores_job(duck) -> None:
    """Scheduler entrypoint (default-OFF; enabling/wiring is owned by the
    caller -- this module never touches scheduler/jobs.py or config.py).
    Mirrors app.ml.snapshot.run_snapshot_job exactly: offload the blocking
    fit+write to a thread so the event loop is never stalled, log a one-line
    summary. Persists ONLY the ml_vol_scores table -- nothing here reads or
    writes any bot/trading state, so this job cannot change trading
    behaviour."""
    import asyncio

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, _score_and_persist, duck)
    logging.getLogger(log_name).info(
        "vol scores %s: %s rows across %s symbols (%s skipped)",
        res["as_of"], res["rows"], res["symbols"], res["skipped"],
    )


# --- CLI ----------------------------------------------------------------------


def _market_db_path() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "market.duckdb"


def _main() -> int:
    ap = argparse.ArgumentParser(description="Per-name daily volatility scorer (PLAN §13.9 step 2)")
    ap.add_argument("--horizons", default="5,21")
    ap.add_argument("--symbols", default=None,
                     help="comma-separated tickers; default = the 147-name reference panel")
    ap.add_argument("--uni-db", default=None)
    ap.add_argument("--main-db", default=None)
    ap.add_argument("--as-of", default=None, help="score as of this date (YYYY-MM-DD), default = latest")
    ap.add_argument("--dry-run", action="store_true", help="score and print only, never write")
    a = ap.parse_args()

    horizons = tuple(int(x) for x in a.horizons.split(","))
    symbols = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else list(REFERENCE_PANEL)

    main_path = Path(a.main_db) if a.main_db else _market_db_path()
    uni_reader, uni_con = _open_universe_reader(a.uni_db)
    main_con = duckdb.connect(str(main_path), read_only=True) if main_path.exists() else None
    main_reader = _RawReader(main_con) if main_con is not None else None

    skips: list[dict] = []
    df = score_universe(
        symbols, horizons, uni_reader=uni_reader, main_reader=main_reader,
        as_of=a.as_of, skip_report=skips,
    )

    # Close all read connections before (maybe) opening a writer below.
    if uni_con is not None:
        uni_con.close()
    if main_con is not None:
        main_con.close()

    print(f"=== VOL SCORES ===  {len(symbols)} requested, {len(df)} rows scored, "
          f"{len(skips)} symbol/horizon skips, dry_run={a.dry_run}")
    if not df.empty:
        with pd.option_context("display.max_rows", 30, "display.width", 160):
            sample = df.sort_values(["horizon", "pctile"], ascending=[True, False]).head(15)
            print(sample.to_string(index=False))
    if skips:
        print(f"\nskipped ({len(skips)}, showing up to 15):")
        for s in skips[:15]:
            print(" ", s)

    if a.dry_run:
        print("\ndry-run: nothing written")
    elif df.empty:
        print("\nnothing to write (empty result)")
    else:
        from app.db.duck import DuckStore

        duck = DuckStore(main_path)
        try:
            n = write_scores(duck, df)
        finally:
            duck.close()
        print(f"\nwrote {n} rows to ml_vol_scores in {main_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
