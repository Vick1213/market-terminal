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


def hedge_for(symbol: str) -> tuple[str, float, float] | None:
    return DEFAULT_HEDGES.get(symbol.upper())


def build_plan(
    regime: str | None,
    vol_signal: dict | None,
    *,
    base_stop_pct: float,
    base_tp_pct: float,
    base_hedge_ratio: float,
) -> dict:
    """Deterministic intraday risk envelope from regime + the vol-overlay forecast.

    Tightens size and requires the hedge when vol is elevated / regime is risk-off;
    loosens (modestly) when calm. Pure arithmetic — no LLM, safe to call every tick.
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

    # widen the stop a touch in stress (more intraday noise), keep TP:risk ~2:1
    stop_pct = round(base_stop_pct * (1.25 if stress else 1.0), 3)
    tp_pct = round(base_tp_pct, 3)

    return {
        "bias": bias,
        "regime": regime,
        "vol_regime": vol_regime,
        "vol_percentile": vp,
        "risk_scale": round(risk_scale, 2),
        "stop_pct": stop_pct,
        "tp_pct": tp_pct,
        "hedge_ratio": round(base_hedge_ratio, 2),
        # outside a clean risk-on tape, only take trades that can be hedged.
        "require_hedge": bias != "risk-on",
        "note": (
            f"{bias} tape, vol {('%d%%-ile' % round(vp * 100)) if vp is not None else 'n/a'} "
            f"→ size×{risk_scale:.2f}, stop {stop_pct:.2f}% / tp {tp_pct:.2f}%, "
            f"hedge {'required' if bias != 'risk-on' else 'optional'}"
        ),
    }
