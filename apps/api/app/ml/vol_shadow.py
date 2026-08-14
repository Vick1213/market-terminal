"""Shadow-mode vol annotations on bot decisions (PLAN §13.9 step 4, §13.6).

Phase A **changes nothing about trading behaviour**. Every function here is
called strictly AFTER a sleeve has already finished deciding qty/notional/
stop/status — it only reads the already-final decision and records what the
per-name vol rule (``app.ml.vol_scores``, ``ml_vol_scores``) WOULD have done,
so §13.6's promotion criteria can later be graded on live evidence
(``vol_grade.py``) instead of a backtest IC. Nothing in this module writes to
``bot_proposals``, ``bot_orders``, or any field a sleeve reads back to make a
decision — day sleeve annotations live only inside the pre-existing
``day_signal_journal.context`` JSON blob (extended with one ``"vol"`` key);
swing annotations live in a brand-new ``ml_vol_shadow`` table nothing else
reads. Every public entrypoint is fail-soft: missing/stale scores, an absent
``ml_vol_scores`` table, or any other error collapses to
``{"available": False, "reason": ...}`` and is NEVER raised into a bot code
path (mirrors the fee-gate discipline every other trading module here already
uses for its own reads).

**Reference-panel level (PLAN §13.5).** ``pred_vol`` is read at
``horizon=21`` only — the level-admissible horizon (§13.3/§13.4): h=5 levels
are not sizeable, so this module never scales off them.

**Counterfactual sizing formula — inverse-vol RELATIVE to the sleeve's own
sizing** (documented in full on ``day_vol_shadow``/``swing_vol_shadow``):

    scale = clip(panel_median_pred_vol_h21 / symbol_pred_vol_h21, 0.25, 4.0)

``panel_median_pred_vol_h21`` is the cross-sectional median h=21 ``pred_vol``
(already shrinkage-calibrated, §13.4) across the 147-name reference panel on
the SAME scoring ``ts`` as the symbol's own row. A name at the panel's typical
vol gets ``scale=1.0``; jumpier names scale down, calmer names scale up. This
reweights the dollar size the sleeve ALREADY decided on rather than deriving
a new absolute vol-target size — Phase A must not invent a new sizing model,
only shadow the existing one. ``[0.25, 4.0]`` is a judgment call (PLAN §13
specifies no exact bound) guarding against one outlier producing an absurd
multiplier; it borrows the *spirit* of ``vol_scores._CALIB_BAND`` without
claiming its statistical backing.

Run (dry, prints the fetch_vol_context/counterfactual shape for one symbol
against the live DB, never writes): none — this module has no CLI; see
``vol_grade.py`` for the grading/reporting entrypoint.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

log_name = "ml.vol_shadow"

try:
    from app.ml.vol_scores import LEVEL_ADMISSIBLE_HORIZON as SCALE_HORIZON
except Exception:  # pragma: no cover - defensive, mirrors vol_scores.py's own
    # fallback-on-import-failure convention so this module never hard-fails
    # just because a sibling in-flight module changed shape.
    SCALE_HORIZON = 21

# Mirrors app.edge.strategist_tools._VOL_RANK_STALE_TRADING_DAYS's convention
# (weekday count strictly between the score's date and now) -- duplicated
# rather than imported so this module stays decoupled from the LLM-tool file
# (see test_vol_rank_tool.py's analogous decoupling note).
STALE_TRADING_DAYS = 5

# Judgment call (PLAN §13 gives no exact bound): clip the inverse-vol scale
# factor so one extreme low/high-vol name can't produce an absurd multiplier.
_SCALE_CLIP = (0.25, 4.0)

# Minimum reference-panel members with a valid h=21 level score on the SAME
# ts before trusting their median as the scaling denominator -- mirrors
# vol_scores._CALIB_MIN_SYMBOLS's "too thin a pool to trust" reasoning.
_MIN_REFERENCE_PANEL_N = 20


def _trading_days_stale(ts: Any, now: datetime) -> int:
    """Weekday count strictly between ``ts``'s date and ``now``'s date. Exact
    duplicate of app.edge.strategist_tools._trading_days_stale's logic (see
    that function's docstring for the no-market-holiday-calendar caveat)."""
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts[:19])
    d0, d1 = ts.date(), now.date()
    if d1 <= d0:
        return 0
    days = 0
    d = d0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _fetch_score_row(duck, symbol: str, horizon: int) -> tuple[dict | None, str | None]:
    """Latest ``ml_vol_scores`` row for (symbol, horizon). Returns
    ``(row_dict, None)`` or ``(None, reason)`` -- never raises."""
    try:
        row = duck.fetchone(
            "SELECT ts, estimator, pred_vol, level_admissible, rank, pctile, "
            "in_reference_panel, n_obs FROM ml_vol_scores WHERE symbol = ? AND horizon = ? "
            "ORDER BY ts DESC LIMIT 1",
            [symbol, horizon],
        )
    except Exception as exc:  # missing table, corrupt DB, etc.
        return None, f"ml_vol_scores unavailable: {type(exc).__name__}: {exc}"
    if row is None:
        return None, "no_score"
    (ts, estimator, pred_vol, level_admissible, rank, pctile, in_panel, n_obs) = row
    if not level_admissible:
        return None, f"h={horizon} level not admissible for sizing (PLAN §13.3)"
    if pred_vol is None or not np.isfinite(float(pred_vol)) or float(pred_vol) <= 0:
        return None, "pred_vol missing or non-positive"
    return {
        "ts": ts, "estimator": estimator, "pred_vol": float(pred_vol),
        "rank": int(rank) if rank is not None else None,
        "pctile": float(pctile) if pctile is not None else None,
        "in_reference_panel": bool(in_panel),
        "n_obs": int(n_obs) if n_obs is not None else None,
    }, None


def _panel_reference_level(duck, ts: Any, horizon: int) -> tuple[float | None, int]:
    """Cross-sectional median h=``horizon`` ``pred_vol`` across reference-panel
    members scored at the SAME ``ts`` as the symbol being annotated. Returns
    ``(None, n)`` (n = contributing rows found, possibly 0) when there are
    fewer than ``_MIN_REFERENCE_PANEL_N`` -- never raises."""
    try:
        rows = duck.fetchall(
            "SELECT pred_vol FROM ml_vol_scores WHERE horizon = ? AND ts = ? "
            "AND in_reference_panel = TRUE AND level_admissible = TRUE AND pred_vol IS NOT NULL",
            [horizon, ts],
        )
    except Exception:
        return None, 0
    vals = [float(r[0]) for r in rows if r[0] is not None and np.isfinite(float(r[0])) and float(r[0]) > 0]
    if len(vals) < _MIN_REFERENCE_PANEL_N:
        return None, len(vals)
    return float(np.median(vals)), len(vals)


def fetch_vol_context(duck, symbol: str | None, *, horizon: int = SCALE_HORIZON,
                       now: datetime | None = None) -> dict:
    """Shared "raw" vol context both sleeves annotate with -- the symbol's
    latest level-admissible score plus the panel-relative scale factor.
    NEVER raises; every failure mode collapses to ``available=False`` (score
    missing/stale/table absent) with ``reason`` explaining why, per the
    fail-soft contract this whole module follows."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    out: dict[str, Any] = {
        "available": False, "reason": None, "symbol": symbol, "horizon": horizon,
        "ts": None, "age_trading_days": None, "stale": None, "estimator": None,
        "pred_vol": None, "pctile": None, "rank": None, "in_reference_panel": None,
        "n_obs": None, "panel_reference_pred_vol": None, "panel_reference_n": None,
        "scale_factor": None,
    }
    try:
        if not symbol:
            out["reason"] = "no_symbol"
            return out
        score, reason = _fetch_score_row(duck, symbol, horizon)
        if score is None:
            out["reason"] = reason
            return out
        age = _trading_days_stale(score["ts"], now)
        stale = age > STALE_TRADING_DAYS
        out.update({
            "available": True,
            "ts": str(score["ts"])[:10],
            "age_trading_days": age,
            "stale": stale,
            "estimator": score["estimator"],
            "pred_vol": score["pred_vol"],
            "pctile": score["pctile"],
            "rank": score["rank"],
            "in_reference_panel": score["in_reference_panel"],
            "n_obs": score["n_obs"],
        })
        if stale:
            out["reason"] = (
                f"score is {age} trading day(s) old (> {STALE_TRADING_DAYS}-day staleness "
                "threshold) -- counterfactual not computed off a stale forecast"
            )
            return out
        ref_level, ref_n = _panel_reference_level(duck, score["ts"], horizon)
        out["panel_reference_n"] = ref_n
        if ref_level is None:
            out["reason"] = (
                f"insufficient reference-panel coverage at this ts "
                f"({ref_n} < {_MIN_REFERENCE_PANEL_N}) -- counterfactual not computed"
            )
            return out
        out["panel_reference_pred_vol"] = ref_level
        raw_scale = ref_level / score["pred_vol"]
        if not np.isfinite(raw_scale) or raw_scale <= 0:
            out["reason"] = "non-finite or non-positive scale factor"
            return out
        lo, hi = _SCALE_CLIP
        out["scale_factor"] = float(min(max(raw_scale, lo), hi))
        return out
    except Exception as exc:  # belt-and-suspenders: this function must never raise
        out["available"] = False
        out["reason"] = f"error: {type(exc).__name__}: {exc}"
        return out


# --- day sleeve --------------------------------------------------------------


def day_vol_shadow(duck, decision: dict, min_risk_dollars: float, *, now: datetime | None = None) -> dict:
    """Shadow annotation for ONE day-sleeve decision, called from
    ``DayTraderService._journal`` AFTER the trade decision (act/qty/notional/
    stop) is already final -- this function only reads ``decision``, never
    mutates it, and its return value is stashed under a new ``"vol"`` key in
    the JSON ``context`` blob that already gets journaled. Nothing here can
    feed back into what the bot did.

    Counterfactual: qty/notional are rescaled by ``fetch_vol_context``'s
    ``scale_factor`` (see module docstring for the formula); the STOP
    DISTANCE is deliberately held FIXED at the bot's own actual distance
    (``entry - sl_price`` on the primary leg). §13.7 describes per-name SIZE
    scaling as the Phase B day wiring ("replace the 3-tier risk_scale with a
    continuous per-name scale"); a vol-scaled STOP is a separate, still
    undesigned mechanic mentioned only as a risk to check, never specified --
    inventing one here would be a second, unvalidated modelling choice riding
    on top of the vol score, exactly what §13.3 already warns against for h=5
    sizing. So only qty/notional move, which means ``risk_d = stop_dist *
    qty`` changes ONLY through qty -- precisely what lets this annotation
    re-check the $5 min-risk fee gate (``config.day_min_risk_dollars``) AFTER
    vol scaling, per §13.6 criterion 4 / §13.7's required check order.

    Only computed for ACTED decisions with a primary leg (entry + sl_price
    both present) -- a skipped/blocked tick never had a real qty/stop to
    rescale, so counterfactual is left ``None`` with a reason instead of
    fabricating one.
    """
    out: dict[str, Any] = {"available": False, "reason": None}
    try:
        symbol = decision.get("symbol")
        ctx = fetch_vol_context(duck, symbol, now=now)
        out.update(ctx)
        out["counterfactual"] = None
        out["counterfactual_reason"] = None
        if not ctx["available"]:
            return out
        if ctx.get("scale_factor") is None:
            out["counterfactual_reason"] = ctx.get("reason") or "scale_factor_unavailable"
            return out

        legs = decision.get("legs") or []
        primary = next((lg for lg in legs if lg.get("role") == "primary"), None)
        # `_build_long`/`_build_short` inherit `decision["act"]` (True for every
        # selected candidate going in) and only ever override it to False on an
        # early-return failure path -- those failure paths never attach `legs`
        # either, so `act and primary is not None` is exactly "this decision
        # produced a real, sized, stopped trade".
        acted = bool(decision.get("act")) and primary is not None
        if not acted:
            out["counterfactual_reason"] = "decision_not_acted"
            return out

        entry = primary.get("entry")
        sl = primary.get("sl_price")
        if entry is None or sl is None or float(entry) <= 0:
            out["counterfactual_reason"] = "primary leg missing entry/sl_price"
            return out
        entry = float(entry)
        stop_dist = abs(entry - float(sl))
        if stop_dist <= 0:
            out["counterfactual_reason"] = "zero actual stop distance"
            return out

        asset_class = decision.get("asset_class")
        actual_qty = primary.get("qty")
        actual_notional = primary.get("notional")
        if actual_qty is not None:
            actual_qty = float(actual_qty)
            if actual_notional is None:
                actual_notional = actual_qty * entry
        if actual_notional is None:
            actual_notional = decision.get("notional")
        if actual_notional is None or float(actual_notional) <= 0:
            out["counterfactual_reason"] = "no actual notional to scale"
            return out
        actual_notional = float(actual_notional)
        actual_qty_eff = actual_qty if actual_qty is not None else (actual_notional / entry)

        scale = ctx["scale_factor"]
        cf_notional_raw = actual_notional * scale
        if asset_class == "equity":
            cf_qty = math.floor(cf_notional_raw / entry)
            cf_notional = cf_qty * entry
        else:  # crypto: fractional, mirrors the bot's own notional-based sizing
            cf_qty = cf_notional_raw / entry
            cf_notional = cf_notional_raw

        actual_risk_dollars = stop_dist * actual_qty_eff
        cf_risk_dollars = stop_dist * cf_qty
        out["counterfactual"] = {
            "stop_dist": round(stop_dist, 6),
            "actual_qty": round(actual_qty_eff, 6),
            "actual_notional": round(actual_notional, 2),
            "actual_risk_dollars": round(actual_risk_dollars, 4),
            "actual_clears_fee_gate": bool(actual_risk_dollars >= min_risk_dollars),
            "cf_qty": round(cf_qty, 6),
            "cf_notional": round(cf_notional, 2),
            "cf_risk_dollars": round(cf_risk_dollars, 4),
            "cf_clears_fee_gate": bool(cf_risk_dollars >= min_risk_dollars),
            "min_risk_dollars": min_risk_dollars,
            "scale_factor": scale,
        }
        return out
    except Exception as exc:  # never raise into the day sleeve's decision path
        return {"available": False, "reason": f"error: {type(exc).__name__}: {exc}",
                "counterfactual": None, "counterfactual_reason": None}


# --- swing sleeve --------------------------------------------------------------

_SHADOW_DDL = """
CREATE TABLE IF NOT EXISTS ml_vol_shadow (
    proposal_id              INTEGER NOT NULL,
    symbol                   TEXT NOT NULL,
    ts                       TEXT NOT NULL,
    run_id                   TEXT,
    score_available          INTEGER NOT NULL DEFAULT 0,
    score_ts                 TEXT,
    score_age_trading_days   INTEGER,
    score_stale              INTEGER,
    estimator                TEXT,
    pred_vol                 REAL,
    pctile                   REAL,
    rank                     INTEGER,
    in_reference_panel       INTEGER,
    n_obs                    INTEGER,
    panel_reference_pred_vol REAL,
    panel_reference_n        INTEGER,
    scale_factor             REAL,
    counterfactual_available INTEGER NOT NULL DEFAULT 0,
    reason                   TEXT,
    detail                   TEXT NOT NULL,
    created_at                TEXT NOT NULL,
    PRIMARY KEY (proposal_id, symbol, ts)
);
"""


def ensure_shadow_table(sqlite) -> None:
    """Idempotent create -- also declared in db/schema.py::init_sqlite (the
    convention app.ml.vol_scores.ensure_table already follows for its own
    DuckDB table), kept here too so a bare/temp SqliteStore (tests) works
    without running the full schema bootstrap."""
    sqlite.execute(_SHADOW_DDL)


def swing_vol_shadow(duck, proposal: dict, available_cash: float | None,
                      *, now: datetime | None = None) -> dict:
    """Shadow annotation for ONE swing proposal, called from
    ``TradingBotService.propose()`` AFTER every ``bot_proposals`` row is
    already inserted (``proposal['id']`` set) -- reads ``proposal`` only,
    never mutates it, and nothing downstream (``execute()``, guardrails,
    ``blocks``/``status``) ever reads this annotation back.

    Counterfactual — same inverse-vol-relative-to-current-sizing rule as the
    day sleeve (module docstring), applied to whichever field the swing
    sizer actually populated (buys are dollar ``notional``, sells are share
    ``qty`` — see ``bot.build_proposals``):

        BUY : cf_notional = min(actual_notional * scale, available_cash)
        SELL: cf_qty      = actual_qty * min(scale, 1.0)

    Cash-only judgment calls (PLAN §13.7/§13.8: swing must never imply
    margin):
      * BUYS are capped at ``available_cash`` — the SAME batch-level cash
        figure ``build_proposals`` returns. Checking each proposal against
        the full pool independently, rather than replaying the funding
        pass's sequential draw-down across the whole run, is a deliberate
        simplification for a hypothetical number: replaying that funding
        ORDER here would itself be re-deriving a decision (which proposals
        get funded first), which this phase must not do even in a shadow
        calculation.
      * SELLS never scale up (``min(scale, 1.0)``) — scaling a sell above
        what the bot already planned would require re-confirming free
        (non-margin) shares beyond ``proposal['qty']`` (itself already
        capped at currently-held sleeve shares by ``build_proposals``), and
        this annotation has no side-effect-free way to re-verify that bound.
        Never implying a bigger sell means never implying a short.

    Swing has no per-share bracket stop (unlike the day sleeve) — its
    "stop distance" is the fixed ``stop_pct`` per bucket already baked into
    ``proposal['max_loss_est'] = target_value * stop_pct / 100``. Phase A
    doesn't touch stops (§13.6), so ``stop_pct`` stays fixed; the implied
    DOLLAR stop distance scales proportionally with the vol-scaled dollar
    size: ``cf_stop_dollars = cf_order_value * stop_pct / 100``.
    """
    out: dict[str, Any] = {"available": False, "reason": None}
    try:
        symbol = proposal.get("symbol")
        ctx = fetch_vol_context(duck, symbol, now=now)
        out.update(ctx)
        out["counterfactual"] = None
        out["counterfactual_reason"] = None
        if not ctx["available"]:
            return out
        if ctx.get("scale_factor") is None:
            out["counterfactual_reason"] = ctx.get("reason") or "scale_factor_unavailable"
            return out

        side = (proposal.get("side") or "").lower()
        scale = ctx["scale_factor"]
        actual_notional = proposal.get("notional")
        actual_qty = proposal.get("qty")
        order_value = abs(proposal.get("delta_value") or 0.0) or None
        target_value = proposal.get("target_value") or 0.0
        max_loss_est = proposal.get("max_loss_est")
        stop_pct = (
            (max_loss_est / target_value * 100.0)
            if (max_loss_est is not None and target_value) else None
        )

        cash_capped = False
        if side == "buy":
            if not actual_notional:
                out["counterfactual_reason"] = "no actual notional to scale (buy)"
                return out
            actual_notional = float(actual_notional)
            cf_notional_raw = actual_notional * scale
            if available_cash is not None:
                cash_capped = cf_notional_raw > available_cash + 1e-6
                cf_notional = min(cf_notional_raw, max(0.0, available_cash))
            else:
                cf_notional = cf_notional_raw
            cf_qty = None
            cf_order_value = cf_notional
        elif side == "sell":
            if not actual_qty:
                out["counterfactual_reason"] = "no actual qty to scale (sell)"
                return out
            actual_qty = float(actual_qty)
            eff_scale = min(scale, 1.0)  # sells never scale up -- see docstring
            cf_qty = actual_qty * eff_scale
            cf_notional = None
            cf_order_value = (
                (cf_qty / actual_qty) * order_value if (order_value and actual_qty) else None
            )
        else:
            out["counterfactual_reason"] = f"unhandled side {side!r}"
            return out

        actual_stop_dollars = max_loss_est
        cf_stop_dollars = (
            cf_order_value * stop_pct / 100.0
            if (cf_order_value is not None and stop_pct is not None) else None
        )

        out["counterfactual"] = {
            "side": side,
            "scale_factor": scale,
            "actual_notional": actual_notional if side == "buy" else None,
            "actual_qty": actual_qty if side == "sell" else None,
            "actual_stop_pct": round(stop_pct, 4) if stop_pct is not None else None,
            "actual_stop_dollars": actual_stop_dollars,
            "cf_notional": round(cf_notional, 2) if cf_notional is not None else None,
            "cf_qty": round(cf_qty, 6) if cf_qty is not None else None,
            "cf_stop_dollars": round(cf_stop_dollars, 2) if cf_stop_dollars is not None else None,
            "cash_capped": cash_capped,
            "available_cash": available_cash,
        }
        return out
    except Exception as exc:  # never raise into the swing propose() path
        return {"available": False, "reason": f"error: {type(exc).__name__}: {exc}",
                "counterfactual": None, "counterfactual_reason": None}


def write_swing_shadow(sqlite, proposal_id: int, symbol: str, ts: str,
                        run_id: str | None, shadow: dict) -> None:
    """Idempotent upsert into ``ml_vol_shadow``, keyed on
    ``(proposal_id, symbol, ts)`` per the PLAN §13.6 spec."""
    ensure_shadow_table(sqlite)
    cf = shadow.get("counterfactual") or None
    sqlite.execute(
        "INSERT OR REPLACE INTO ml_vol_shadow (proposal_id, symbol, ts, run_id, "
        "score_available, score_ts, score_age_trading_days, score_stale, estimator, "
        "pred_vol, pctile, rank, in_reference_panel, n_obs, panel_reference_pred_vol, "
        "panel_reference_n, scale_factor, counterfactual_available, reason, detail, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            proposal_id, symbol, ts, run_id,
            1 if shadow.get("available") else 0,
            shadow.get("ts"), shadow.get("age_trading_days"),
            (1 if shadow.get("stale") else 0) if shadow.get("stale") is not None else None,
            shadow.get("estimator"), shadow.get("pred_vol"), shadow.get("pctile"), shadow.get("rank"),
            (1 if shadow.get("in_reference_panel") else 0)
            if shadow.get("in_reference_panel") is not None else None,
            shadow.get("n_obs"), shadow.get("panel_reference_pred_vol"), shadow.get("panel_reference_n"),
            shadow.get("scale_factor"), 1 if cf else 0,
            shadow.get("reason") or shadow.get("counterfactual_reason"),
            json.dumps(shadow, default=str),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ],
    )


def annotate_and_persist_swing(duck, sqlite, proposals: list[dict], available_cash: float | None,
                                run_id: str, ts: str) -> int:
    """Annotate + write every proposal in ``proposals`` (each already carries
    its persisted ``id``). Called from ``TradingBotService.propose()`` AFTER
    the ``bot_proposals`` insert loop — purely additive I/O, never touches
    ``proposals`` or any field a caller reads back. Fail-soft per-proposal:
    one bad annotation is logged and skipped, never raised, so it can never
    take down a ``propose()`` run. Returns the number of rows written."""
    n = 0
    for p in proposals:
        try:
            pid = p.get("id")
            symbol = p.get("symbol")
            if pid is None or not symbol:
                continue
            shadow = swing_vol_shadow(duck, p, available_cash)
            write_swing_shadow(sqlite, pid, symbol, ts, run_id, shadow)
            n += 1
        except Exception:
            logging.getLogger(log_name).debug(
                "vol shadow annotation failed for %s", p.get("symbol"), exc_info=True)
            continue
    return n
