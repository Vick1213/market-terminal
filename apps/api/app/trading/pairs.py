"""Relative-strength PAIRS engine for the day sleeve — pure functions over bars.

Uses the PREDEFINED pair list (``intraday.PAIRS``, decided beforehand, not
discovered live). For each pair (a, b) it measures the intraday return SPREAD
(a%-move minus b%-move). When the spread is wide enough, it longs the relatively
STRONG leg and shorts the relatively WEAK leg — a sector-/factor-neutral trade
that isolates the idiosyncratic divergence (the common market move nets out).

No I/O here — the day trader feeds in the bars and decides execution; this just
picks the single widest actionable pair this tick.
"""
from __future__ import annotations


def _intraday_ret_pct(bars: list[dict]) -> float | None:
    """Intraday % move over the bar window (open of first bar → last close)."""
    if not bars:
        return None
    first = bars[0].get("o") or bars[0].get("c")
    last = bars[-1].get("c")
    if not first or last is None:
        return None
    return (last - first) / first * 100.0


def best_pair_trade(
    bars_by_sym: dict[str, list[dict]],
    pairs: list[tuple[str, str]],
    *,
    min_spread_pct: float,
    is_flat,
) -> dict | None:
    """The single widest actionable pair this tick, or None.

    ``bars_by_sym`` maps symbol → 1-min bars. ``is_flat(sym)`` returns True when
    the day sleeve currently holds no position in that name (we only OPEN a fresh,
    balanced pair — never leg into one we're already half in). Returns
    ``{long, short, spread, strength, detail}`` for the widest pair whose absolute
    return spread clears ``min_spread_pct``."""
    best: dict | None = None
    for a, b in pairs:
        ra = _intraday_ret_pct(bars_by_sym.get(a))
        rb = _intraday_ret_pct(bars_by_sym.get(b))
        if ra is None or rb is None:
            continue
        if not (is_flat(a) and is_flat(b)):
            continue
        spread = ra - rb
        if abs(spread) < min_spread_pct:
            continue
        strong, weak = (a, b) if spread > 0 else (b, a)
        strength = abs(spread)
        if best is None or strength > best["strength"]:
            best = {
                "long": strong, "short": weak,
                "spread": round(spread, 3), "strength": round(strength, 3),
                "detail": (f"pair {strong}↑/{weak}↓: rel-strength spread "
                           f"{spread:+.2f}% (long strong, short weak — sector-neutral)"),
            }
    return best
