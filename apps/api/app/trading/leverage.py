"""Conviction-gated leverage for the day sleeve.

Leverage multiplies whatever edge you have — including a negative one — so this
module is deliberately conservative:

  1. It is OFF unless the env master (``day_leverage_enabled``) AND the panel
     toggle (``bot_config.day_leverage``) are both on.
  2. Even then, by default it stays at 1x until the edge is VALIDATED — a
     cost-adjusted positive expectancy over a real sample (``is_edge_validated``).
     Leverage on an unproven edge just amplifies the losses.
  3. A single trade only earns leverage when it clears a strict "high signal"
     gate (setup that works + top conviction + strong signal + risk-on regime +
     calm vol), and the multiplier is hard-capped per-position and gross.

All functions are pure/read-only (no orders) — the day trader calls
``leverage_armed`` once per tick and ``leverage_for`` per candidate; the sizing
and the gross cap live in ``daytrader._segment``.
"""

from __future__ import annotations

import logging

log = logging.getLogger("market.trading.leverage")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cost_adjusted_expectancy(sqlite, settings, *, min_n: int = 1) -> tuple[float | None, int]:
    """Average $ P&L per closed acted day trade AFTER modeled costs (round-trip
    slippage in bps of notional + per-trade commission). Paper fills are free, so
    the raw journal P&L overstates the live edge — this is the number that must be
    positive before leverage is allowed. Returns (expectancy, n)."""
    rows = sqlite.fetchall(
        "SELECT pnl, notional, qty, last_price FROM day_signal_journal "
        "WHERE decision = 'acted' AND outcome IN ('win','loss','flat') AND pnl IS NOT NULL")
    slip = float(getattr(settings, "day_slippage_bps", 5.0)) / 1e4
    comm = float(getattr(settings, "day_commission_per_trade", 0.0))
    net = []
    for r in rows:
        pnl = _f(r["pnl"])
        if pnl is None:
            continue
        notional = _f(r["notional"])
        if notional is None:
            q, p = _f(r["qty"]), _f(r["last_price"])
            notional = (q * p) if (q and p) else 0.0
        # Round-trip: slippage on entry AND exit notional + commission per side.
        cost = abs(notional) * slip * 2.0 + comm * 2.0
        net.append(pnl - cost)
    if len(net) < max(1, min_n):
        return None, len(net)
    return round(sum(net) / len(net), 4), len(net)


def is_edge_validated(sqlite, settings) -> tuple[bool, str]:
    """True only when the cost-adjusted expectancy is positive over at least
    ``day_leverage_min_validation_trades`` closed trades. This is the gate that
    keeps leverage dormant until the strategy has actually earned it."""
    min_n = int(getattr(settings, "day_leverage_min_validation_trades", 100))
    exp, n = cost_adjusted_expectancy(sqlite, settings, min_n=min_n)
    if n < min_n:
        return False, f"not validated — only {n}/{min_n} cost-adjusted trades"
    if exp is None or exp <= 0:
        return False, f"not validated — cost-adjusted expectancy {exp} <= 0 over {n} trades"
    return True, f"validated — cost-adjusted expectancy +{exp}$/trade over {n} trades"


def leverage_armed(sqlite, settings, cfg: dict) -> tuple[bool, str]:
    """Whether leverage may be deployed AT ALL right now. Requires the env master,
    the panel toggle, and (unless disabled) a validated edge. Returns (armed,
    reason) — reason is surfaced so the UI/journal can explain why it's 1x."""
    if not bool(getattr(settings, "day_leverage_enabled", False)):
        return False, "leverage off (env master day_leverage_enabled=false)"
    if not cfg.get("leverage"):
        return False, "leverage off (panel toggle)"
    if bool(getattr(settings, "day_leverage_require_validated", True)):
        ok, why = is_edge_validated(sqlite, settings)
        if not ok:
            return False, why
        return True, why
    return True, "leverage armed (validation gate disabled)"


def leverage_for(decision: dict, plan: dict, settings, armed: bool) -> tuple[float, str]:
    """The leverage multiplier for ONE opening candidate, in [1.0, day_max_leverage].

    Returns 1.0 (no leverage) unless ``armed`` AND the trade clears the strict
    high-signal gate: an allowed setup, top-tier conviction, strong signal, a
    risk-on regime, and calm forecast vol. When it qualifies, the multiplier ramps
    with conviction from 1.0 up to the cap. Equities only. Returns (lev, reason)."""
    if not armed:
        return 1.0, "not armed"
    if (decision.get("asset_class") or "equity") != "equity":
        return 1.0, "leverage is equities-only"
    sig = decision.get("signal") or {}
    kind = sig.get("kind")
    allowed = getattr(settings, "day_leverage_setups", ["breakout", "momentum"])
    if kind not in allowed:
        return 1.0, f"setup '{kind}' not leverage-eligible"
    conv = _f(decision.get("conviction")) or 0.0
    strength = _f(sig.get("strength")) or 0.0
    conv_min = float(getattr(settings, "day_leverage_conviction_min", 1.5))
    str_min = float(getattr(settings, "day_leverage_min_strength", 2.5))
    if conv < conv_min:
        return 1.0, f"conviction {conv:.2f} < {conv_min} bar"
    if strength < str_min:
        return 1.0, f"signal strength {strength:.2f} < {str_min} bar (need confluence)"
    if not plan.get("regime") == "risk-on" and plan.get("bias") != "risk-on":
        return 1.0, "leverage only in a risk-on regime"
    vp = plan.get("vol_percentile")
    vmax = float(getattr(settings, "day_leverage_vol_pctile_max", 0.5))
    if vp is not None and vp > vmax:
        return 1.0, f"forecast vol {vp:.0%}-ile > {vmax:.0%} — too choppy to lever"
    # Qualifies: ramp leverage with how far conviction clears the bar, to the cap.
    cap = float(getattr(settings, "day_max_leverage", 2.0))
    span = max(conv_min, 1e-6)
    ramp = min(1.0, max(0.0, (conv - conv_min) / span))
    lev = round(1.0 + (cap - 1.0) * ramp, 3)
    lev = max(1.0, min(cap, lev))
    return lev, (f"HIGH SIGNAL: {kind} conv {conv:.2f} / str {strength:.2f}, risk-on, "
                 f"vol {'%d%%' % round(vp*100) if vp is not None else 'n/a'} → {lev}x")
