"""Point-in-time feature matrix (PLAN §12.2 / §12.3) from DuckDB.

One row per ``(date, asset)``; columns are a snapshot of every *usable* stored
signal, each **lagged to the moment it was actually knowable** and joined to the
trading calendar with a backward as-of merge on its **release date**. A model
that sees a value before it was published is cheating (§12.3) — this module is
the only thing standing between the zoo and that cheat.

────────────────────────────────────────────────────────────────────────────
INVENTORY REALITY (probed on data/market.duckdb, 2026-06-28 — NOT the plan's
aspirational signal list). Honest scope of what is *actually populated with
multi-year history* and therefore modellable:

  Targets (ts_price, source 'yahoo'):  SPY, QQQ — daily OHLCV, 2021-06-10 →
      present, ~1267 rows. This is the binding window; everything pre-2021-06
      is unusable because the *target* doesn't exist there.

  Features with real depth:
    ts_macro 'fred'  : DGS10/DGS2/DFII10/T10Y2Y/T10Y3M/T10YIE (2015+ daily),
                       DCOILWTICO/DTWEXBGS/DFF/RRPONTSYD (daily), SOFR (2018+),
                       NFCI/ANFCI/WALCL/WRESBAL/WTREGEN (weekly), M2SL (monthly),
                       BAMLC0A0CM/BAMLH0A0HYM2 (OAS, only 2023-06+ → ~half the
                       SPY window is NaN; included but coverage-flagged).
    ts_macro 'cboe'  : VIX, VIX3M, PC_EQUITY/INDEX/TOTAL.
    ts_macro 'naaim' : NAAIM_EXPOSURE (weekly).
    ts_cot           : E-MINI S&P / VIX / USD INDEX … (weekly, 2023-03+).
    ts_price derived : trailing momentum / realised-vol / RSI of the target.

  Probed-but-NOT-usable on this DB (present in code / memory, absent or <6mo of
  rows here → deliberately excluded, not silently all-NaN): every Phase-16
  ingestor output (TFF macro scalars, Kalshi, Cleveland nowcast, EIA,
  policy-risk, fed_speeches, treasury auctions, EDGAR tables — those dedicated
  tables don't even exist on this DB yet), the new FRED term-premium/nowcast
  series, the dead ``computed`` CORR_* (n≈4, frozen 2026-06-15), coinpaprika
  (n=5), AAII/retail (start 2026-06), divergence_snapshots (n=2),
  gex_snapshots (n=4), ts_short_interest (empty), strategist_snapshots (n=4).

  ⇒ The plan's rich signal list does NOT exist in queryable form yet. The spec
    below is **declarative**: when those ingestors actually populate this DB,
    adding a column is a one-line ``FeatureSpec`` entry. Until then the builder
    refuses to fabricate all-NaN columns and reports what it skipped.
────────────────────────────────────────────────────────────────────────────

Release-lag calibration (calendar days from the row's stamped reference date to
the first session the value is usable *at the close*):
  * FRED ts is the **observation/reference** date, NOT the publish date, and the
    stored ts_macro value is the latest *revised* value. For NFCI/ANFCI (the
    revision-prone financial-conditions family that drove the apparent edge) we
    now load ALFRED point-in-time first-print vintages from ts_macro_vintage and
    join on the **actual publication date** (pit=True specs) — fully revision-
    leak-safe. For the other revised series we still mitigate only the timing
    leak with conservative publication lags. See REVISION_CAVEAT below.
  * FRED daily rates (H.15 etc.): published ~4:15pm the next business day → +1d.
  * H.4.1 balance sheet (WALCL/WRESBAL/WTREGEN, Wed ref): Thu 4:30pm → +2d.
  * NFCI/ANFCI (Fri ref): released the following Wed → +5d.
  * M2SL (1st-of-month ref): ~3–4 weeks later → +30d.
  * CBOE VIX/VIX3M: official close, knowable at the close → +0d.
  * CBOE put/call: posted EOD/next morning → +1d.
  * NAAIM (Wed survey): Thu → +1d.
  * COT (Tue report): Fri 15:30 ET → +4d (conservative: usable next Monday).

Pure numpy/pandas/duckdb. No scaling / ranking / imputation here — those are
*fitted* transforms and per §12.2 must be fit inside each CV fold. This module
emits only PIT-aligned **raw levels** plus stateless trailing price features
(causal per row, no fitted parameters, so fold-safe).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

REVISION_CAVEAT = (
    "NFCI/ANFCI now use ALFRED point-in-time first-print vintages (pit=True, from "
    "ts_macro_vintage) — revision leak resolved for the family that carried the "
    "apparent edge. Doing so collapsed NFCI's forward-IC from ~0.2 (revised) to "
    "~0.06 (PIT) ⇒ the EDGE_FOUND verdict was ~60% revision leakage. Other revised "
    "FRED series (WALCL/WRESBAL/WEI/M2/OAS) are still latest-revised; release lags "
    "fix their timing leak but not revision leak — extend pit=True to any that "
    "prove material per PLAN §12.3."
)

Table = Literal["ts_macro", "ts_cot"]


# --- feature specification --------------------------------------------------


@dataclass(frozen=True)
class FeatureSpec:
    """One declarative, PIT-safe feature column.

    name            : output column.
    table           : source table.
    key             : series_id (ts_macro) or market_code (ts_cot).
    field           : for ts_cot, the derived quantity (see _load_cot).
    release_lag_days: calendar days from the stamped reference date to the first
                      session the value is knowable at the close. The single
                      most important number for leakage safety.
    """

    name: str
    table: Table
    key: str
    release_lag_days: int
    field: str = "value"
    pit: bool = False  # load ALFRED point-in-time vintages (revision-leak-safe)


# Asset-independent ("broadcast") macro/positioning features. Only series with
# real multi-year depth on this DB (see INVENTORY REALITY above).
MACRO_SPECS: tuple[FeatureSpec, ...] = (
    # FRED daily rates / spreads (H.15, next-business-day publish → +1d).
    FeatureSpec("dgs10", "ts_macro", "DGS10", 1),
    FeatureSpec("dgs2", "ts_macro", "DGS2", 1),
    FeatureSpec("dfii10", "ts_macro", "DFII10", 1),
    FeatureSpec("t10y2y", "ts_macro", "T10Y2Y", 1),
    FeatureSpec("t10y3m", "ts_macro", "T10Y3M", 1),
    FeatureSpec("t10yie", "ts_macro", "T10YIE", 1),
    FeatureSpec("wti", "ts_macro", "DCOILWTICO", 1),
    FeatureSpec("usd_broad", "ts_macro", "DTWEXBGS", 1),
    FeatureSpec("fed_funds", "ts_macro", "DFF", 1),
    FeatureSpec("rrp", "ts_macro", "RRPONTSYD", 1),
    FeatureSpec("sofr", "ts_macro", "SOFR", 1),
    # FRED credit OAS — only 2023-06+ on this DB; coverage-flagged at build.
    FeatureSpec("oas_ig", "ts_macro", "BAMLC0A0CM", 1),
    FeatureSpec("oas_hy", "ts_macro", "BAMLH0A0HYM2", 1),
    # FRED weekly financial-conditions / balance sheet. NFCI/ANFCI are heavily
    # revised: the latest-revised value leaks future information (its weekly
    # *change* rank-correlates only ~0.45 with the genuinely-knowable first
    # print). pit=True loads ALFRED initial-release vintages from
    # ts_macro_vintage and joins on the actual publication date — verified
    # 2026-06-28 to collapse the apparent NFCI edge from IC~0.2 to ~0.06 (= the
    # honest deployable number). Falls back to the lagged revised value if no
    # vintage rows are present (e.g. the table hasn't been backfilled).
    FeatureSpec("nfci", "ts_macro", "NFCI", 5, pit=True),
    FeatureSpec("anfci", "ts_macro", "ANFCI", 5, pit=True),
    FeatureSpec("walcl", "ts_macro", "WALCL", 2),
    FeatureSpec("wresbal", "ts_macro", "WRESBAL", 2),
    FeatureSpec("tga", "ts_macro", "WTREGEN", 2),
    # FRED monthly money supply.
    FeatureSpec("m2", "ts_macro", "M2SL", 30),
    # CBOE vol complex (official close → same-day knowable).
    FeatureSpec("vix", "ts_macro", "VIX", 0),
    FeatureSpec("vix3m", "ts_macro", "VIX3M", 0),
    # CBOE put/call posted EOD/next morning → +1d.
    FeatureSpec("pc_equity", "ts_macro", "PC_EQUITY", 1),
    FeatureSpec("pc_index", "ts_macro", "PC_INDEX", 1),
    FeatureSpec("pc_total", "ts_macro", "PC_TOTAL", 1),
    # NAAIM manager exposure (weekly survey).
    FeatureSpec("naaim", "ts_macro", "NAAIM_EXPOSURE", 1),
    # COT positioning (Tue report, Fri release → +4d). Net non-commercial and
    # net commercial for the three macro-relevant contracts present on this DB.
    FeatureSpec("cot_es_noncomm_net", "ts_cot", "13874A", 4, field="noncomm_net"),
    FeatureSpec("cot_es_comm_net", "ts_cot", "13874A", 4, field="comm_net"),
    FeatureSpec("cot_vix_noncomm_net", "ts_cot", "1170E1", 4, field="noncomm_net"),
    FeatureSpec("cot_usd_noncomm_net", "ts_cot", "098662", 4, field="noncomm_net"),
    # --- Phase-16 backfilled deep-history signals (added 2026-06-28) --------
    # CBOE/yahoo vol complex — official EOD index values, knowable at close → +0d.
    FeatureSpec("move", "ts_macro", "MOVE", 0),        # bond-market vol ("bond VIX")
    FeatureSpec("vvix", "ts_macro", "VVIX", 0),        # vol-of-vol
    FeatureSpec("vix9d", "ts_macro", "VIX9D", 0),      # 9-day front vol
    FeatureSpec("skew", "ts_macro", "SKEW", 0),        # 30d tail-risk premium
    # FRED daily rates decomposition / credit (next-business-day publish → +1d).
    FeatureSpec("term_prem", "ts_macro", "THREEFYTP10", 1),     # Kim-Wright 10y term premium
    FeatureSpec("infl_5y5y", "ts_macro", "T5YIFR", 1),         # 5y5y fwd inflation
    FeatureSpec("breakeven_5y", "ts_macro", "T5YIE", 1),       # 5y breakeven
    FeatureSpec("cp_a2p2", "ts_macro", "RIFSPPNA2P2D30NB", 1), # A2/P2 30d CP rate (funding stress)
    # FRED weekly real-activity nowcast (Sat ref, Thu release → +6d).
    FeatureSpec("wei", "ts_macro", "WEI", 6),
    # Policy-risk indices. GPR monthly (~mid-following-month publish → +45d);
    # TPU daily (policyuncertainty.com, ~1–2d lag → +2d).
    FeatureSpec("gpr", "ts_macro", "GPR", 45),
    FeatureSpec("tpu", "ts_macro", "TPU", 2),
    # CFTC TFF positioning (Tue report, Fri release → +4d). Asset-Manager and
    # Leveraged-Money net for the macro-relevant contracts; the AM−LM
    # divergence (real-money vs hedge-fund) is engineered in features.py.
    FeatureSpec("tff_es_am", "ts_macro", "TFF_ES_AM_NET", 4),
    FeatureSpec("tff_es_lm", "ts_macro", "TFF_ES_LM_NET", 4),
    FeatureSpec("tff_nq_am", "ts_macro", "TFF_NQ_AM_NET", 4),
    FeatureSpec("tff_nq_lm", "ts_macro", "TFF_NQ_LM_NET", 4),
    FeatureSpec("tff_ust10y_am", "ts_macro", "TFF_UST10Y_AM_NET", 4),
    FeatureSpec("tff_ust10y_lm", "ts_macro", "TFF_UST10Y_LM_NET", 4),
    FeatureSpec("tff_vix_am", "ts_macro", "TFF_VIX_AM_NET", 4),
    FeatureSpec("tff_vix_lm", "ts_macro", "TFF_VIX_LM_NET", 4),
)

# Minimum observations a spec needs before we trust it as a column (else it is
# reported as skipped rather than joined as a mostly-NaN column).
MIN_OBS = 60


# --- DuckDB loaders ---------------------------------------------------------


def _load_macro(duck, series_id: str) -> pd.DataFrame:
    rows = duck.fetchall(
        "SELECT ts, value FROM ts_macro WHERE series_id = ? AND value IS NOT NULL "
        "ORDER BY ts",
        [series_id],
    )
    if not rows:
        return pd.DataFrame(columns=["ref_date", "value"])
    df = pd.DataFrame(rows, columns=["ref_date", "value"])
    df["ref_date"] = pd.to_datetime(df["ref_date"]).dt.normalize()
    return df


def _load_macro_pit(duck, series_id: str) -> pd.DataFrame:
    """ALFRED point-in-time first-print vintages for a revised series.

    Returns ``[ref_date, release_date, value]`` — ``release_date`` is the actual
    publication date the value became knowable (not a modelled lag), so a
    backward as-of join on it is revision-leak-safe by construction. Empty if the
    vintage table hasn't been backfilled (caller falls back to the revised value).
    """
    rows = duck.fetchall(
        "SELECT ref_date, release_date, value FROM ts_macro_vintage "
        "WHERE series_id = ? AND value IS NOT NULL ORDER BY release_date",
        [series_id],
    )
    if not rows:
        return pd.DataFrame(columns=["ref_date", "release_date", "value"])
    df = pd.DataFrame(rows, columns=["ref_date", "release_date", "value"])
    df["ref_date"] = pd.to_datetime(df["ref_date"]).dt.normalize()
    df["release_date"] = pd.to_datetime(df["release_date"]).dt.normalize()
    return df


def _load_cot(duck, market_code: str, field: str) -> pd.DataFrame:
    rows = duck.fetchall(
        "SELECT ts, noncomm_long, noncomm_short, comm_long, comm_short, open_interest "
        "FROM ts_cot WHERE market_code = ? ORDER BY ts",
        [market_code],
    )
    if not rows:
        return pd.DataFrame(columns=["ref_date", "value"])
    df = pd.DataFrame(
        rows,
        columns=["ref_date", "ncl", "ncs", "cl", "cs", "oi"],
    )
    df["ref_date"] = pd.to_datetime(df["ref_date"]).dt.normalize()
    if field == "noncomm_net":
        df["value"] = df["ncl"] - df["ncs"]
    elif field == "comm_net":
        df["value"] = df["cl"] - df["cs"]
    else:
        raise ValueError(f"unknown cot field {field!r}")
    return df[["ref_date", "value"]]


def _load_prices(duck, symbol: str, source: str = "yahoo") -> pd.DataFrame:
    rows = duck.fetchall(
        "SELECT ts, open, high, low, close, volume FROM ts_price "
        "WHERE symbol = ? AND source = ? AND close IS NOT NULL ORDER BY ts",
        [symbol, source],
    )
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df.drop_duplicates("date", keep="last").reset_index(drop=True)


# --- the release-time as-of join (the leakage-critical primitive) -----------


class LookaheadError(AssertionError):
    """Raised when a feature value's declared release date is later than the
    decision date it was joined onto — i.e. a look-ahead leak slipped through.
    """


def release_join(
    calendar: pd.DatetimeIndex,
    feature: pd.DataFrame,
    release_lag_days: int,
    *,
    col: str = "value",
) -> pd.DataFrame:
    """Backward as-of join of ``feature`` onto ``calendar`` on **release date**.

    ``feature`` has ``[ref_date, <col>]``; its release date is
    ``ref_date + release_lag_days`` — UNLESS the frame already carries an
    explicit ``release_date`` column (ALFRED point-in-time vintages), in which
    case that actual publication date is used verbatim and the lag is ignored.
    For each calendar day ``t`` the result holds the value of the most recent
    observation **released on or before t**. Returns columns
    ``[<col>, _release_date]`` indexed by the calendar — the retained release
    date lets :func:`assert_pit_safe` verify no leak.

    By construction (``direction='backward'``) no value with release_date > t
    can ever be selected, so the join is leak-safe *for the declared lag*; the
    lag itself is the modelling assumption documented in MACRO_SPECS.
    """
    cal = pd.DataFrame({"date": pd.DatetimeIndex(calendar).normalize()}).sort_values("date")
    if feature.empty:
        cal[col] = np.nan
        cal["_release_date"] = pd.NaT
        return cal.set_index("date")
    feat = feature.copy()
    if "release_date" in feat.columns:
        feat["_release_date"] = pd.to_datetime(feat["release_date"]).dt.normalize()
    else:
        feat["_release_date"] = feat["ref_date"] + pd.Timedelta(days=release_lag_days)
    # merge_asof requires identical key dtypes; DuckDB DATE vs the price-derived
    # TIMESTAMP calendar can differ in resolution (s vs us). Pin both to ns.
    cal["date"] = cal["date"].astype("datetime64[ns]")
    feat["_release_date"] = feat["_release_date"].astype("datetime64[ns]")
    feat = feat.sort_values("_release_date")
    merged = pd.merge_asof(
        cal,
        feat[["_release_date", col]].rename(columns={col: "_val"}),
        left_on="date",
        right_on="_release_date",
        direction="backward",
    )
    out = pd.DataFrame(
        {col: merged["_val"].to_numpy(), "_release_date": merged["_release_date"].to_numpy()},
        index=pd.DatetimeIndex(merged["date"]),
    )
    return out


def assert_pit_safe(joined: pd.DataFrame, release_col: str = "_release_date") -> None:
    """Guard: every retained release date must be ≤ its decision date.

    This is the programmatic leakage check. A correct :func:`release_join` can
    never violate it; the guard exists to catch a *corrupted* matrix (a
    mis-declared negative lag, a future-stamped observation, or a hand-edited
    test fixture) before it ever reaches a model.
    """
    rd = joined[release_col]
    dates = pd.DatetimeIndex(joined.index)
    bad = rd.notna() & (pd.DatetimeIndex(rd) > dates)
    if bool(np.asarray(bad).any()):
        n = int(np.asarray(bad).sum())
        first = dates[np.asarray(bad)][0]
        raise LookaheadError(
            f"{n} row(s) hold a value released AFTER the decision date "
            f"(first at {first.date()}): look-ahead leak."
        )


# --- stateless trailing price features (causal, fold-safe) ------------------


def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).rolling(window, min_periods=window).mean()
    down = (-delta.clip(upper=0)).rolling(window, min_periods=window).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def price_features(prices: pd.DataFrame) -> pd.DataFrame:
    """Trailing, knowable-at-close price features for one asset (§12.2, 'kept
    on a short leash'). All causal (use only prices up to and including ``t``):
    no fitted parameters, so they are fold-safe and need no release lag.
    """
    df = prices.set_index("date")
    close = df["close"]
    logr = np.log(close).diff()
    out = pd.DataFrame(index=df.index)
    out["ret_1d"] = logr
    out["mom_21"] = np.log(close) - np.log(close.shift(21))
    out["mom_63"] = np.log(close) - np.log(close.shift(63))
    out["rvol_21"] = logr.rolling(21, min_periods=10).std()
    out["rsi_14"] = _rsi(close, 14)
    sma200 = close.rolling(200, min_periods=100).mean()
    out["dist_200dma"] = close / sma200 - 1.0
    return out


# --- top-level builder ------------------------------------------------------


@dataclass
class BuildReport:
    """What the builder actually assembled vs skipped — surfaces the inventory
    reality (mostly-empty plan signals) at runtime instead of silently.
    """

    used: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # name -> reason
    coverage: dict[str, float] = field(default_factory=dict)  # name -> non-null frac
    n_rows: int = 0
    assets: list[str] = field(default_factory=list)
    start: str = ""
    end: str = ""
    caveats: list[str] = field(default_factory=list)


def build_feature_matrix(
    duck,
    assets: tuple[str, ...] = ("SPY", "QQQ"),
    *,
    price_source: str = "yahoo",
    macro_specs: tuple[FeatureSpec, ...] = MACRO_SPECS,
    min_obs: int = MIN_OBS,
    validate: bool = True,
) -> tuple[pd.DataFrame, BuildReport]:
    """Assemble the PIT ``(date, asset)`` feature matrix.

    Steps: per asset, take its trading calendar from ts_price; release-join
    every broadcast macro/COT spec onto that calendar; add the asset's own
    trailing price features; stack assets into a MultiIndex frame. Specs that
    are absent or thinner than ``min_obs`` are *skipped and reported*, never
    fabricated as all-NaN. With ``validate`` the PIT guard runs on every join.

    Returns ``(matrix, report)``. The matrix carries raw PIT levels only —
    scaling/ranking/imputation are deferred to the in-fold pipeline (§12.2).
    """
    report = BuildReport(assets=list(assets), caveats=[REVISION_CAVEAT])

    # Pre-load + qualify macro specs once (they're asset-independent).
    qualified: list[tuple[FeatureSpec, pd.DataFrame]] = []
    for spec in macro_specs:
        if spec.table == "ts_macro":
            if spec.pit:
                feat = _load_macro_pit(duck, spec.key)
                if feat.empty:  # vintages not backfilled → fall back to revised
                    feat = _load_macro(duck, spec.key)
                    report.caveats.append(
                        f"{spec.name}: no ALFRED vintages on DB → using "
                        f"latest-revised value (revision leak NOT mitigated)."
                    )
            else:
                feat = _load_macro(duck, spec.key)
        elif spec.table == "ts_cot":
            feat = _load_cot(duck, spec.key, spec.field)
        else:  # pragma: no cover - guarded by Literal
            report.skipped[spec.name] = f"unknown table {spec.table}"
            continue
        if len(feat) < min_obs:
            report.skipped[spec.name] = f"only {len(feat)} obs (< {min_obs})"
            continue
        qualified.append((spec, feat))

    frames: list[pd.DataFrame] = []
    for asset in assets:
        prices = _load_prices(duck, asset, source=price_source)
        if prices.empty:
            report.skipped[f"<asset:{asset}>"] = "no price rows"
            continue
        calendar = pd.DatetimeIndex(prices["date"])
        cols: dict[str, pd.Series] = {}

        for spec, feat in qualified:
            joined = release_join(calendar, feat, spec.release_lag_days)
            if validate:
                assert_pit_safe(joined)
            cols[spec.name] = joined["value"]

        pf = price_features(prices)
        for c in pf.columns:
            cols[f"px_{c}"] = pf[c].reindex(calendar)

        adf = pd.DataFrame(cols, index=calendar)
        adf.index.name = "date"
        adf["asset"] = asset
        adf = adf.reset_index().set_index(["date", "asset"])
        frames.append(adf)

    if not frames:
        return pd.DataFrame(), report

    matrix = pd.concat(frames).sort_index()
    report.n_rows = len(matrix)
    report.used = [c for c in matrix.columns]
    report.coverage = {
        c: float(matrix[c].notna().mean()) for c in matrix.columns
    }
    if not matrix.empty:
        dates = matrix.index.get_level_values("date")
        report.start = str(dates.min().date())
        report.end = str(dates.max().date())
    return matrix, report
