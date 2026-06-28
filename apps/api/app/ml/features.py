"""Stationary feature engineering + cross-asset block (PLAN §12.2).

``dataset.build_feature_matrix`` emits the leakage-safe *raw PIT levels*. That
is correct for provenance but wrong for modelling: most of those columns
(yields, balance-sheet, price levels, WTI) have a **unit root**. Feeding raw
levels to a tree/linear model is the price-prediction trap one rung up — the
model sees "today ≈ yesterday", learns nothing transferable, and the OOS IC
collapses (exactly what phase-2 v1 showed). §12.2 prescribes the fix:
regime-relative z-scores, deltas/velocities, and a few ratios/interactions.

This module turns the raw matrix into a **stationary, economically-motivated**
feature set and adds the deep cross-asset price block (TLT / gold / copper /
DXY / semis / cyclical-vs-defensive) that was sitting unused in ts_price.

Leakage discipline is preserved:
  * Every transform is **causal/trailing** (``diff``, ``pct_change``, rolling
    z over a *trailing* window) — it uses only data up to and including ``t``,
    so it is knowable at the close of ``t`` and needs no extra release lag
    beyond the one ``dataset`` already applied to the underlying series.
  * Trailing z has no *fitted global* parameter (the window is per-row,
    backward-looking), so it does not leak test-fold statistics. The in-fold
    StandardScaler (pipeline.py) still does the cross-sectional standardisation
    §12.2 wants fit-inside-the-fold.
  * Cross-asset closes are official daily closes (lag 0) forward-filled onto the
    target calendar — a forward-fill carries the *last known* close, which is
    knowable, so it stays PIT-safe.

Output columns are ``eng_*`` (engineered macro/cross-asset) and ``px_*`` (the
target's own trailing price features, already stationary). All raw
non-stationary levels are dropped. Pure numpy/pandas.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.ml.dataset import (
    BuildReport,
    MACRO_SPECS,
    _load_prices,
    build_feature_matrix,
)

# Deep, free, PIT-trivially-safe cross-asset symbols (all yahoo, 2021-06+).
CROSS_ASSETS = {
    "tlt": "TLT",          # long bonds — risk-off / duration
    "gld": "GLD",          # gold
    "copper": "HG=F",      # copper — cyclical growth
    "dxy": "DX-Y.NYB",     # dollar index
    "smh": "SMH",          # semis — risk leadership
    "xly": "XLY",          # consumer discretionary
    "xlp": "XLP",          # consumer staples (defensive)
}


def _trailing_z(s: pd.Series, win: int = 252, minp: int = 126) -> pd.Series:
    """Regime-relative z-score over a *trailing* window (causal, PIT-safe)."""
    mu = s.rolling(win, min_periods=minp).mean()
    sd = s.rolling(win, min_periods=minp).std()
    return (s - mu) / sd.replace(0, np.nan)


def _mom(close: pd.Series, k: int) -> pd.Series:
    return np.log(close) - np.log(close.shift(k))


def _cross_asset_block(duck, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """Trailing momentum + cyclical/defensive ratios from the cross-asset
    universe, aligned (ffill) onto the target ``calendar``. Broadcast columns
    (identical for every target on a given date), like the macro block.
    """
    closes: dict[str, pd.Series] = {}
    for key, sym in CROSS_ASSETS.items():
        px = _load_prices(duck, sym, source="yahoo")
        if px.empty:
            continue
        s = px.set_index("date")["close"].sort_index()
        # last-known close forward-filled onto the target calendar (PIT-safe).
        closes[key] = s.reindex(s.index.union(calendar)).ffill().reindex(calendar)

    out = pd.DataFrame(index=calendar)
    for key, s in closes.items():
        out[f"eng_xa_{key}_mom21"] = _mom(s, 21)
    # Economically-motivated relative-strength spreads.
    if "copper" in closes and "gld" in closes:
        out["eng_xa_copper_gold"] = _mom(closes["copper"], 21) - _mom(closes["gld"], 21)
    if "xly" in closes and "xlp" in closes:
        out["eng_xa_xly_xlp"] = _mom(closes["xly"], 21) - _mom(closes["xlp"], 21)
    if "smh" in closes:
        out["eng_xa_smh_mom63"] = _mom(closes["smh"], 63)
    return out


def _engineer_macro(raw: pd.DataFrame) -> pd.DataFrame:
    """Stationary transforms of one asset's raw broadcast macro columns.

    ``raw`` is the date-indexed slice for a single asset from
    ``dataset.build_feature_matrix`` (raw PIT levels). Returns a date-indexed
    frame of ``eng_*`` stationary features. Missing source columns are skipped
    gracefully (the DB may not carry every spec).
    """
    g = lambda c: raw[c] if c in raw.columns else pd.Series(np.nan, index=raw.index)
    e = pd.DataFrame(index=raw.index)

    # --- rates & curve (changes; spreads kept as levels) -------------------
    e["eng_dgs10_chg5"] = g("dgs10").diff(5)
    e["eng_dgs2_chg5"] = g("dgs2").diff(5)
    e["eng_curve_2s10s"] = g("t10y2y")
    e["eng_curve_2s10s_chg5"] = g("t10y2y").diff(5)
    e["eng_curve_3m10y"] = g("t10y3m")
    e["eng_realrate_chg21"] = g("dfii10").diff(21)
    e["eng_breakeven_chg5"] = g("t10yie").diff(5)

    # --- credit ------------------------------------------------------------
    e["eng_credit_hyig"] = g("oas_hy") - g("oas_ig")
    e["eng_oas_hy_chg5"] = g("oas_hy").diff(5)
    e["eng_oas_hy_z"] = _trailing_z(g("oas_hy"))

    # --- vol / risk --------------------------------------------------------
    e["eng_vix_z"] = _trailing_z(g("vix"))
    e["eng_vix_ts"] = g("vix") / g("vix3m") - 1.0   # term structure: >0 = stress
    e["eng_vix_chg5"] = g("vix").diff(5)
    e["eng_pc_total_z"] = _trailing_z(g("pc_total"))
    e["eng_pc_equity"] = g("pc_equity")

    # --- liquidity (levels are non-stationary → use % / abs changes) -------
    e["eng_walcl_chg21pct"] = g("walcl").pct_change(21)
    e["eng_reserves_chg21pct"] = g("wresbal").pct_change(21)
    e["eng_rrp_chg21"] = g("rrp").diff(21)
    e["eng_tga_chg21"] = g("tga").diff(21)
    e["eng_m2_yoy"] = g("m2").pct_change(252)

    # --- financial conditions ---------------------------------------------
    e["eng_nfci"] = g("nfci")             # already a standardised index
    e["eng_nfci_chg5"] = g("nfci").diff(5)
    e["eng_anfci"] = g("anfci")

    # --- positioning -------------------------------------------------------
    e["eng_naaim"] = g("naaim") / 100.0
    e["eng_naaim_chg5"] = g("naaim").diff(5)
    e["eng_cot_es_z"] = _trailing_z(g("cot_es_noncomm_net"))
    e["eng_cot_es_comm_z"] = _trailing_z(g("cot_es_comm_net"))
    e["eng_cot_vix_z"] = _trailing_z(g("cot_vix_noncomm_net"))

    # --- fx / commodity ----------------------------------------------------
    e["eng_usd_chg21"] = g("usd_broad").pct_change(21)
    e["eng_wti_chg21"] = g("wti").pct_change(21)

    # --- Phase-16 backfilled signals (deep history, orthogonal info) -------
    # Vol complex beyond equity VIX.
    e["eng_move_z"] = _trailing_z(g("move"))            # bond-market vol
    e["eng_move_chg5"] = g("move").diff(5)
    e["eng_vvix_z"] = _trailing_z(g("vvix"))            # vol-of-vol
    e["eng_vvix_vix"] = g("vvix") / g("vix")            # vol-of-vol vs vol ratio
    e["eng_vix9d_ts"] = g("vix9d") / g("vix") - 1.0     # very-front term structure
    e["eng_skew_z"] = _trailing_z(g("skew"))            # tail-risk premium
    # Rates decomposition / inflation expectations.
    e["eng_term_prem"] = g("term_prem")                 # 10y term premium (level)
    e["eng_term_prem_chg21"] = g("term_prem").diff(21)
    e["eng_infl_5y5y"] = g("infl_5y5y")                 # 5y5y fwd inflation (level)
    e["eng_breakeven5y_chg5"] = g("breakeven_5y").diff(5)
    # Funding stress: A2/P2 commercial-paper rate over fed funds.
    e["eng_cp_funding"] = g("cp_a2p2") - g("fed_funds")
    # Real-activity nowcast + policy uncertainty.
    e["eng_wei"] = g("wei")                             # weekly econ index (growth %)
    e["eng_wei_chg5"] = g("wei").diff(5)
    e["eng_gpr_z"] = _trailing_z(g("gpr"))              # geopolitical risk
    e["eng_tpu_z"] = _trailing_z(g("tpu"))              # trade-policy uncertainty
    e["eng_tpu_chg21"] = g("tpu").diff(21)
    # CFTC TFF real-money-vs-hedge-fund divergence (AM − LM), z-scored. Only
    # 2023-05+ on this DB → NaN earlier; in-fold median impute handles it.
    e["eng_tff_es_div_z"] = _trailing_z(g("tff_es_am") - g("tff_es_lm"))
    e["eng_tff_nq_div_z"] = _trailing_z(g("tff_nq_am") - g("tff_nq_lm"))
    e["eng_tff_ust10y_div_z"] = _trailing_z(g("tff_ust10y_am") - g("tff_ust10y_lm"))
    e["eng_tff_vix_div_z"] = _trailing_z(g("tff_vix_am") - g("tff_vix_lm"))
    return e


def build_model_matrix(
    duck,
    assets: tuple[str, ...] = ("SPY", "QQQ"),
) -> tuple[pd.DataFrame, BuildReport]:
    """Leakage-safe raw PIT matrix → stationary, model-ready feature matrix.

    Reuses ``dataset.build_feature_matrix`` (so the release-time joins + PIT
    guard still run), then engineers stationary macro features per asset, adds
    the broadcast cross-asset block, and keeps the target's own ``px_*``
    trailing features. Raw non-stationary levels are dropped. Returns the same
    ``(matrix, report)`` contract; the report's ``used``/``coverage`` are
    rewritten to the engineered columns.
    """
    raw, report = build_feature_matrix(duck, assets=assets)
    if raw.empty:
        return raw, report

    frames: list[pd.DataFrame] = []
    for asset in assets:
        if asset not in raw.index.get_level_values("asset"):
            continue
        rasset = raw.xs(asset, level="asset").sort_index()
        cal = pd.DatetimeIndex(rasset.index)

        eng = _engineer_macro(rasset)
        xa = _cross_asset_block(duck, cal)
        px = rasset[[c for c in rasset.columns if c.startswith("px_")]]

        adf = pd.concat([eng, xa, px], axis=1)
        adf.index.name = "date"
        adf["asset"] = asset
        adf = adf.reset_index().set_index(["date", "asset"])
        frames.append(adf)

    matrix = pd.concat(frames).sort_index()
    report.used = list(matrix.columns)
    report.coverage = {c: float(matrix[c].notna().mean()) for c in matrix.columns}
    report.caveats.append(
        "features engineered to stationary form (§12.2): changes / trailing-z / "
        "ratios + cross-asset block; raw unit-root levels dropped."
    )
    return matrix, report


# Re-export so train.py can hash a stable feature identity.
__all__ = ["build_model_matrix", "CROSS_ASSETS", "MACRO_SPECS"]
