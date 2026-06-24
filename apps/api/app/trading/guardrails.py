"""Code-enforced guardrails for every proposed order.

Pure functions, no I/O — so they are trivially testable and can never be
talked around by an LLM prompt. The bot calls ``evaluate_order`` for each
proposed trade; a non-empty return is a hard block (the proposal is recorded
but never submitted). Caps come from settings (env), so they cannot be widened
from the API at runtime — only the kill switch and mode are runtime-toggleable.

The limits implement the recon's non-negotiables:
  * per-symbol cap as a % of equity AND an absolute notional cap,
  * a daily-loss circuit breaker that halts NEW BUYS (sells still allowed —
    de-risking must never be blocked),
  * a buying-power check so we never fire an order the account can't cover,
  * a dust floor so tiny drifts don't churn the book.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GuardrailConfig:
    max_position_pct: float        # cap any one symbol at this % of equity
    max_position_notional: float   # absolute $ cap per symbol
    min_order_notional: float      # below this, skip (dust), don't trade
    daily_loss_limit_pct: float    # halt new buys if account down this % today
    rebalance_band_pp: float       # ignore target/actual drifts smaller than this
    allow_live: bool = False

    @classmethod
    def from_settings(cls, s) -> "GuardrailConfig":
        return cls(
            max_position_pct=float(s.bot_max_position_pct),
            max_position_notional=float(s.bot_max_position_notional),
            min_order_notional=float(s.bot_min_order_notional),
            daily_loss_limit_pct=float(s.bot_daily_loss_limit_pct),
            rebalance_band_pp=float(s.bot_rebalance_band_pp),
            allow_live=bool(s.bot_allow_live_trading),
        )


def norm_symbol(symbol: str) -> str:
    """Canonical form for matching across Alpaca's slash/no-slash variants
    (orders use 'BTC/USD', positions may come back as 'BTCUSD')."""
    return (symbol or "").upper().replace("/", "").replace("-", "").strip()


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def daily_pnl_pct(account: dict) -> float | None:
    """Today's account P&L %, from equity vs last_equity (previous close).
    None when the broker hasn't populated last_equity yet."""
    equity = _to_float(account.get("equity"))
    last = _to_float(account.get("last_equity"))
    if equity is None or not last:
        return None
    return (equity - last) / last * 100.0


def buys_halted(account: dict, cfg: GuardrailConfig) -> str | None:
    """Reason new buys are halted right now, or None. Triggered by the daily
    loss circuit breaker or a broker-side trading block."""
    if account.get("trading_blocked") or account.get("account_blocked"):
        return "account is trading-blocked by the broker"
    if account.get("trade_suspended_by_user"):
        return "trading suspended by user on the account"
    pnl = daily_pnl_pct(account)
    if pnl is None and cfg.daily_loss_limit_pct > 0:
        # Fail CLOSED: if we can't read today's P&L (no last_equity / previous
        # close), we can't honor the circuit breaker, so don't buy blind.
        return ("cannot determine today's P&L (broker has not reported "
                "last_equity) — buys halted until it does")
    if pnl is not None and pnl <= -cfg.daily_loss_limit_pct:
        return (f"daily-loss circuit breaker: account {pnl:+.1f}% today "
                f"(limit -{cfg.daily_loss_limit_pct:.1f}%) — buys halted")
    return None


def is_dust(order_value: float, cfg: GuardrailConfig) -> bool:
    """Order too small to be worth placing (below the min-order floor)."""
    return order_value < cfg.min_order_notional


def evaluate_order(
    *,
    symbol: str,
    side: str,
    order_value: float,
    current_position_value: float,
    equity: float,
    buying_power: float,
    allowlist: set[str],
    cfg: GuardrailConfig,
    buys_halted_reason: str | None = None,
) -> list[str]:
    """Return the list of guardrail violations for one order (empty = allowed).

    Sells reduce risk, so they bypass the position caps, the buying-power check,
    and the daily-loss halt — only the allowlist applies to them. Buys face the
    full gauntlet.
    """
    blocks: list[str] = []

    if norm_symbol(symbol) not in {norm_symbol(s) for s in allowlist}:
        blocks.append(f"{symbol} not in the bot's allowlist (strategist universe + watchlist)")

    if side == "sell":
        return blocks  # de-risking is always permitted for allowlisted names

    # ---- buys only from here -------------------------------------------------
    if buys_halted_reason:
        blocks.append(buys_halted_reason)

    if order_value > buying_power + 1e-6:
        blocks.append(
            f"order ${order_value:,.0f} exceeds buying power ${buying_power:,.0f}"
        )

    resulting = current_position_value + order_value
    if equity > 0:
        pct = resulting / equity * 100.0
        if pct > cfg.max_position_pct + 1e-6:
            blocks.append(
                f"{symbol} would be {pct:.1f}% of equity, over the "
                f"{cfg.max_position_pct:.0f}% per-position cap"
            )
    if resulting > cfg.max_position_notional + 1e-6:
        blocks.append(
            f"{symbol} position ${resulting:,.0f} would exceed the "
            f"${cfg.max_position_notional:,.0f} per-position notional cap"
        )

    return blocks
