"""Intraday plan — the FAST, deterministic layer the day sleeve trades off.

The user wants intraday trades to be fast and hedged with minimal risk on both
sides, and explicitly NOT to lean on the LLM per trade. So the plan here is pure
arithmetic over signals the terminal already computes (regime + the vol-overlay
forecast): it sets the risk envelope (stop %, take-profit %, size scale) and maps
each single-name long to a market-beta hedge. The day trader then executes
bracketed long+short pairs deterministically — no model call in the hot path.

The hedge realises "minimal risk both sides, high upside if it works": long the
signal name, short a correlated sector/index ETF sized to its beta, so the common
market move nets out and you keep the name's idiosyncratic move. Each leg carries
its OWN bracket (stop-loss + take-profit), so both sides are risk-capped and the
exit is automatic and fast.

ALPACA REALITY (drives what is hedgeable): equities can be shorted and bracketed;
crypto on Alpaca is spot, long-only, market/limit — NO short, NO bracket, NO
futures. So crypto cannot be beta-hedged or stop-bracketed here; it falls back to
a bot-monitored synthetic stop. Index ETFs (SPY/QQQ) ARE the market, so they have
no beta hedge and trade unhedged.
"""
from __future__ import annotations

# Long symbol -> (inverse ETF to BUY as the hedge, beta, ETF leverage). Hedge
# notional = hedge_ratio * beta / leverage * long_notional, side = BUY. The
# leverage divisor right-sizes a -3x ETF so the dollar hedge stays beta-neutral.
# Using an inverse ETF keeps both legs LONG (no short/margin) and hedges crypto.
DEFAULT_HEDGES: dict[str, tuple[str, float, float]] = {
    # semis -> -3x semis (SOXS)
    "NVDA": ("SOXS", 1.0, 3.0), "AMD": ("SOXS", 1.1, 3.0), "AVGO": ("SOXS", 0.9, 3.0),
    "MU": ("SOXS", 1.1, 3.0), "QCOM": ("SOXS", 0.9, 3.0), "SMCI": ("SOXS", 1.6, 3.0),
    "TSM": ("SOXS", 0.9, 3.0),
    # megacap tech -> -1x Nasdaq (PSQ)
    "AAPL": ("PSQ", 1.0, 1.0), "MSFT": ("PSQ", 0.95, 1.0), "AMZN": ("PSQ", 1.1, 1.0),
    "GOOGL": ("PSQ", 1.0, 1.0), "META": ("PSQ", 1.1, 1.0), "TSLA": ("PSQ", 1.4, 1.0),
    "NFLX": ("PSQ", 1.1, 1.0), "CRM": ("PSQ", 1.1, 1.0), "ADBE": ("PSQ", 1.0, 1.0),
    # crypto-proxy equities (high beta) -> -1x Nasdaq (PSQ)
    "COIN": ("PSQ", 1.8, 1.0), "MSTR": ("PSQ", 2.0, 1.0),
    # financials -> -3x financials (FAZ)
    "JPM": ("FAZ", 1.1, 3.0), "BAC": ("FAZ", 1.2, 3.0), "GS": ("FAZ", 1.2, 3.0),
    # energy -> -3x energy (ERY)
    "XOM": ("ERY", 0.9, 3.0), "CVX": ("ERY", 0.9, 3.0),
    # crypto spot -> -1x bitcoin ETF (BITI); equity ETF, so market-hours only.
    "BTC/USD": ("BITI", 1.0, 1.0), "ETH/USD": ("BITI", 1.1, 1.0),
}

# Symbols that ARE the market factor — no beta hedge exists, trade unhedged.
UNHEDGEABLE_INDEX = {"SPY", "QQQ", "DIA", "IWM", "VOO", "IVV"}

# PREDEFINED relative-value pairs (decided beforehand, NOT discovered live — live
# intraday correlation is noisy and overfits). Each pair is same-sector, highly
# correlated, similar beta, both liquid and shortable. The pairs engine longs the
# relatively STRONG leg and shorts the relatively WEAK leg when their intraday
# return spread diverges → sector-neutral, isolating the idiosyncratic move.
# (a, b): trade direction is decided by which leg is stronger that tick.
PAIRS: list[tuple[str, str]] = [
    # semis
    ("NVDA", "AMD"), ("AVGO", "QCOM"), ("TSM", "NVDA"),
    # megacap tech
    ("MSFT", "GOOGL"), ("META", "GOOGL"), ("AMZN", "GOOGL"),
    # financials
    ("JPM", "BAC"), ("GS", "JPM"),
    # energy
    ("XOM", "CVX"),
    # crypto-proxy equities
    ("COIN", "MSTR"),
]
# The broad beta-hedge instruments to SHORT (instead of buying an inverse ETF):
# SPY for a broad book, QQQ for a tech-heavy one. Sized to the day book's net beta.
SHORT_HEDGE_INDEX = {"broad": "SPY", "tech": "QQQ"}


def hedge_for(symbol: str) -> tuple[str, float, float] | None:
    return DEFAULT_HEDGES.get(symbol.upper())


def build_plan(
    regime: str | None,
    vol_signal: dict | None,
    *,
    base_stop_pct: float,
    base_tp_pct: float,
    base_hedge_ratio: float,
    stop_sigma: float = 1.0,
    tp_sigma: float = 3.0,
    trail_sigma: float = 1.5,
    reversion_stop_sigma: float = 0.5,
    min_rr: float = 3.0,
    stop_floor_pct: float = 0.4,
) -> dict:
    """Deterministic intraday risk envelope from regime + the vol-overlay forecast.

    Tightens size and requires the hedge when vol is elevated / regime is risk-off;
    loosens (modestly) when calm. Pure arithmetic — no LLM, safe to call every tick.

    Brackets are σ-multiplier-driven (see ``compute_bracket``): the plan carries the
    multipliers and the min reward:risk gate; the flat ``stop_pct``/``tp_pct`` remain
    only as a fallback for the (rare) tick where the signal has no σ.
    """
    vp = None
    vol_regime = None
    if vol_signal:
        vp = vol_signal.get("vol_percentile")
        vol_regime = vol_signal.get("regime")

    # size scale vs the vol percentile of the forecast (smaller in high vol)
    risk_scale = 1.0
    if vp is not None:
        if vp >= 0.85:
            risk_scale = 0.5
        elif vp >= 0.65:
            risk_scale = 0.75
        elif vp <= 0.30:
            risk_scale = 1.15

    stress = (regime in ("risk-off", "stress")) or (vol_regime == "stress")
    bias = "risk-off" if stress else ("risk-on" if regime == "risk-on" else "neutral")

    # widen the stop a touch in stress (more intraday noise) — both the σ stop
    # multiplier and the fallback % stop. TP/trail multiples are unchanged so the
    # reward:risk ratio actually IMPROVES in stress (wider stop, same target reach).
    stress_mult = 1.25 if stress else 1.0
    stop_pct = round(base_stop_pct * stress_mult, 3)
    tp_pct = round(base_tp_pct, 3)
    stop_sigma_eff = round(stop_sigma * stress_mult, 3)
    rev_stop_sigma_eff = round(reversion_stop_sigma * stress_mult, 3)

    return {
        "bias": bias,
        "regime": regime,
        "vol_regime": vol_regime,
        "vol_percentile": vp,
        "risk_scale": round(risk_scale, 2),
        # fallback flat-% levels (only used when the signal has no σ)
        "stop_pct": stop_pct,
        "tp_pct": tp_pct,
        # σ-multiplier-driven bracket knobs (the primary path)
        "stop_sigma": stop_sigma_eff,
        "tp_sigma": round(tp_sigma, 3),
        "trail_sigma": round(trail_sigma, 3),
        "reversion_stop_sigma": rev_stop_sigma_eff,
        "min_rr": round(min_rr, 3),
        # Minimum stop/trail distance ($) so a σ-tight exit can't sit inside the
        # noise band. Widened in stress alongside the σ stop.
        "stop_floor_pct": round(stop_floor_pct * stress_mult, 4),
        "hedge_ratio": round(base_hedge_ratio, 2),
        # outside a clean risk-on tape, only take trades that can be hedged.
        "require_hedge": bias != "risk-on",
        "note": (
            f"{bias} tape, vol {('%d%%-ile' % round(vp * 100)) if vp is not None else 'n/a'} "
            f"→ size×{risk_scale:.2f}, stop {stop_sigma_eff:.2f}σ / tp {tp_sigma:.2f}σ "
            f"(trail {trail_sigma:.2f}σ), min R:R {min_rr:.1f}, "
            f"hedge {'required' if bias != 'risk-on' else 'optional'}"
        ),
    }


def compute_bracket(signal: dict, entry: float, plan: dict) -> dict:
    """Vol-normalized, structure-aware, setup-differentiated exit for ONE long.

    Returns ``{"ok": True, "exit_type": "trailing"|"bracket", "sl", "tp", "trail_dist",
    "stop_dist", "tp_dist", "rr", "have_sigma", "detail"}`` or ``{"ok": False, "reason"}``
    when a >= ``min_rr`` reward:risk can't be structured (caller then skips the trade).

      * momentum  → stop = max(entry − stop_sigma·σ, just-below win_low) [structure],
                    EXIT via a TRAILING stop (trail = trail_sigma·σ) so winners run.
                    No fixed TP, so the R:R gate uses a tp_sigma·σ reach proxy.
      * reversion → stop = reversion_stop_sigma·σ PAST the win_low extreme (tight),
                    TP = VWAP (the mean it's reverting to) — a normal OCO bracket.
      * no σ      → flat-% fallback, TP widened to satisfy min_rr.
    """
    kind = signal.get("kind")
    sigma = signal.get("sigma")
    win_low = signal.get("win_low")
    vwap = signal.get("vwap")
    min_rr = float(plan.get("min_rr", 3.0))
    have_sigma = sigma is not None and sigma > 0 and entry and entry > 0
    # Alpaca rejects a bracket whose stop_loss isn't strictly BELOW the order's
    # base price ("stop_price must be <= base_price - 0.01"). A σ-tight intraday
    # stop can collapse to within a cent of entry (BAC sl 58.11 vs entry 58.12),
    # then any sub-second drift between the signal bar and the fill makes the live
    # base price cross it → reject. Enforce a minimum gap (≥2¢ or 0.15%) on BOTH
    # legs so the bracket is always valid with margin to spare; the R:R gate below
    # then sees the real (clamped) stop and honestly skips trades that no longer
    # clear min_rr rather than submitting a doomed order.
    min_gap = max(0.02, (entry or 0.0) * 0.0015) if (entry and entry > 0) else 0.02
    # Floor the stop/trail distance so a σ-tight exit can't sit inside the bid/ask
    # bounce (the 0.09%-stop noise-whipsaw the trade audit found). Never below the
    # broker min_gap.
    floor_dist = max(min_gap, (entry or 0.0) * float(plan.get("stop_floor_pct", 0.4)) / 100.0)

    if have_sigma and kind == "reversion":
        extreme = win_low if (win_low is not None and 0 < win_low <= entry) else entry
        sl = min(extreme - plan["reversion_stop_sigma"] * sigma, entry - min_gap)
        stop_dist = entry - sl
        if stop_dist < floor_dist:  # widen a too-tight stop to the noise floor
            stop_dist = floor_dist
            sl = entry - stop_dist
        tp = vwap if (vwap and vwap > entry) else entry + plan["tp_sigma"] * sigma
        tp = max(tp, entry + min_gap)  # keep the target a valid gap above base too
        tp_dist = tp - entry
        if stop_dist <= 0 or tp_dist <= 0:
            return {"ok": False, "reason": "reversion: non-positive stop/target distance"}
        rr = tp_dist / stop_dist
        if rr < min_rr - 1e-9:
            return {"ok": False, "reason": f"reversion R:R {rr:.1f} < {min_rr:.1f} (VWAP too close to fade)"}
        return {"ok": True, "exit_type": "bracket", "sl": sl, "tp": tp, "trail_dist": None,
                "stop_dist": stop_dist, "tp_dist": tp_dist, "rr": rr, "have_sigma": True,
                "detail": (f"reversion: SL {sl:.2f} (win_low−{plan['reversion_stop_sigma']}σ), "
                           f"TP=VWAP {tp:.2f}, R:R {rr:.1f}")}

    if have_sigma:  # momentum (and any non-reversion actionable setup) → trailing
        sigma_sl = entry - plan["stop_sigma"] * sigma
        structure_sl = (win_low * (1 - 0.0005)
                        if (win_low is not None and 0 < win_low < entry) else None)
        sl = max(sigma_sl, structure_sl) if structure_sl is not None else sigma_sl
        # NB: no min_gap clamp here — momentum exits via a TRAILING stop (submitted
        # separately), so this sl level is informational and never hits Alpaca's
        # bracket validation. Clamping it would only shrink the R:R proxy and skip
        # otherwise-valid momentum trades.
        stop_dist = entry - sl
        if stop_dist < floor_dist:  # a too-tight stop is a noise whipsaw — floor it
            stop_dist = floor_dist
            sl = entry - stop_dist
        if stop_dist <= 0:
            return {"ok": False, "reason": "momentum: non-positive stop distance"}
        # Trailing give-back floored too — a sub-noise trail gets stopped on the bounce.
        trail_dist = max(plan["trail_sigma"] * sigma, floor_dist)
        # No fixed TP for a trailing exit, so gate on a tp_sigma·σ reach proxy: the
        # winner must be able to plausibly travel min_rr × the risk before reversing.
        rr = (plan["tp_sigma"] * sigma) / stop_dist
        if rr < min_rr - 1e-9:
            return {"ok": False, "reason": f"momentum R:R proxy {rr:.1f} < {min_rr:.1f} (stop too wide)"}
        return {"ok": True, "exit_type": "trailing", "sl": sl, "tp": None, "trail_dist": trail_dist,
                "stop_dist": stop_dist, "tp_dist": None, "rr": rr, "have_sigma": True,
                "detail": (f"momentum: SL {sl:.2f} ({plan['stop_sigma']}σ/struct), "
                           f"trail {trail_dist:.2f} ({plan['trail_sigma']}σ), R:R proxy {rr:.1f}")}

    # Fallback: no σ available — flat-% bracket, TP widened to honor min_rr.
    stop_pct = float(plan.get("stop_pct", 0.8))
    tp_pct = float(plan.get("tp_pct", 1.6))
    stop_dist = max(entry * stop_pct / 100.0, floor_dist)
    if stop_dist <= 0:
        return {"ok": False, "reason": "fallback: non-positive stop distance"}
    tp_dist = max(entry * tp_pct / 100.0, min_rr * stop_dist, min_gap)
    sl = entry - stop_dist
    tp = entry + tp_dist
    rr = tp_dist / stop_dist
    return {"ok": True, "exit_type": "bracket", "sl": sl, "tp": tp, "trail_dist": None,
            "stop_dist": stop_dist, "tp_dist": tp_dist, "rr": rr, "have_sigma": False,
            "detail": f"fallback %-bracket: SL {sl:.2f}/−{stop_pct:.2f}%, TP {tp:.2f}, R:R {rr:.1f} (no σ)"}


def compute_bracket_short(signal: dict, entry: float, plan: dict) -> dict:
    """The MIRROR of ``compute_bracket`` for a SHORT entry — stop ABOVE, target
    BELOW. Same σ logic, same ``min_rr`` gate, same min-gap clamp (flipped to
    Alpaca's short rule: stop_loss ≥ base+0.01, take_profit ≤ base−0.01). A short's
    loss is unbounded, so the stop is mandatory and the gate is identical.

      * reversion(overbought) → SL = win_high + reversion_stop_sigma·σ (above),
                                TP = VWAP (below) — OCO bracket.
      * momentum(breakdown)   → SL above (σ/structure win_high), TRAILING cover.
      * no σ                  → flat-% fallback.
    """
    kind = signal.get("kind")
    sigma = signal.get("sigma")
    win_high = signal.get("win_high")
    vwap = signal.get("vwap")
    min_rr = float(plan.get("min_rr", 3.0))
    have_sigma = sigma is not None and sigma > 0 and entry and entry > 0
    min_gap = max(0.02, (entry or 0.0) * 0.0015) if (entry and entry > 0) else 0.02
    floor_dist = max(min_gap, (entry or 0.0) * float(plan.get("stop_floor_pct", 0.4)) / 100.0)

    if have_sigma and kind == "reversion":
        extreme = win_high if (win_high is not None and win_high >= entry > 0) else entry
        sl = max(extreme + plan["reversion_stop_sigma"] * sigma, entry + min_gap)
        stop_dist = sl - entry
        if stop_dist < floor_dist:
            stop_dist = floor_dist
            sl = entry + stop_dist
        tp = vwap if (vwap and vwap < entry) else entry - plan["tp_sigma"] * sigma
        tp = min(tp, entry - min_gap)
        tp_dist = entry - tp
        if stop_dist <= 0 or tp_dist <= 0:
            return {"ok": False, "reason": "reversion short: non-positive stop/target distance"}
        rr = tp_dist / stop_dist
        if rr < min_rr - 1e-9:
            return {"ok": False, "reason": f"reversion short R:R {rr:.1f} < {min_rr:.1f} (VWAP too close)"}
        return {"ok": True, "exit_type": "bracket", "sl": sl, "tp": tp, "trail_dist": None,
                "stop_dist": stop_dist, "tp_dist": tp_dist, "rr": rr, "have_sigma": True,
                "detail": (f"short reversion: SL {sl:.2f} (win_high+{plan['reversion_stop_sigma']}σ), "
                           f"TP=VWAP {tp:.2f}, R:R {rr:.1f}")}

    if have_sigma:  # breakdown / non-reversion short → trailing cover
        sigma_sl = entry + plan["stop_sigma"] * sigma
        structure_sl = (win_high * (1 + 0.0005)
                        if (win_high is not None and win_high > entry) else None)
        sl = min(sigma_sl, structure_sl) if structure_sl is not None else sigma_sl
        stop_dist = sl - entry
        if stop_dist < floor_dist:
            stop_dist = floor_dist
            sl = entry + stop_dist
        if stop_dist <= 0:
            return {"ok": False, "reason": "momentum short: non-positive stop distance"}
        trail_dist = max(plan["trail_sigma"] * sigma, floor_dist)
        rr = (plan["tp_sigma"] * sigma) / stop_dist
        if rr < min_rr - 1e-9:
            return {"ok": False, "reason": f"momentum short R:R proxy {rr:.1f} < {min_rr:.1f} (stop too wide)"}
        return {"ok": True, "exit_type": "trailing", "sl": sl, "tp": None, "trail_dist": trail_dist,
                "stop_dist": stop_dist, "tp_dist": None, "rr": rr, "have_sigma": True,
                "detail": (f"short momentum: SL {sl:.2f} ({plan['stop_sigma']}σ/struct), "
                           f"trail {trail_dist:.2f} ({plan['trail_sigma']}σ), R:R proxy {rr:.1f}")}

    stop_pct = float(plan.get("stop_pct", 0.8))
    tp_pct = float(plan.get("tp_pct", 1.6))
    stop_dist = max(entry * stop_pct / 100.0, floor_dist)
    tp_dist = max(entry * tp_pct / 100.0, min_rr * stop_dist, min_gap)
    sl = entry + stop_dist
    tp = entry - tp_dist
    rr = tp_dist / stop_dist
    return {"ok": True, "exit_type": "bracket", "sl": sl, "tp": tp, "trail_dist": None,
            "stop_dist": stop_dist, "tp_dist": tp_dist, "rr": rr, "have_sigma": False,
            "detail": f"short fallback %-bracket: SL {sl:.2f}/+{stop_pct:.2f}%, TP {tp:.2f}, R:R {rr:.1f}"}
