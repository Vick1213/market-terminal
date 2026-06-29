"""Shared MARKET + PORTFOLIO context for the trading sleeves.

The day trader used to reason one name at a time, blind to the rest of the book.
Here we build, once per cycle, two small read-only objects both sleeves (and the
UI) can use so decisions are coherent and explainable:

  * ``MarketContext`` — "what is the market doing": regime, composite, stress
    radar, dealer gamma, vol regime/percentile, breadth, and the strategist's
    current sector tilt (the shared long-term strategy the fast loop should
    respect). This is the answer to "what signals are reaching the day trader".

  * ``PortfolioState`` — "what do I already own and what is my real risk":
    equity, cash, gross exposure, NET BETA (so hedges like PSQ are counted, not
    invisible), sector concentration, swing vs day holdings, and how much day
    risk budget is left.

Pure reads, no order side effects, so this is trivially testable and safe to
call for the UI as well as the trading loop.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.trading.bot import sleeve_holdings
from app.trading.guardrails import norm_symbol
from app.trading.intraday import DEFAULT_HEDGES

BREADTH_SERIES = "BREADTH_PCT_ABOVE_200DMA"

# Inverse / leveraged-short ETFs are held LONG but move OPPOSITE the market, so
# their effective beta-to-SPY is negative. Counting this is what lets net-beta
# hedging work and what made the runaway PSQ pile visible as risk, not noise.
_INVERSE_BETA: dict[str, float] = {
    "SH": -1.0, "PSQ": -1.0, "DOG": -1.0, "RWM": -1.0,
    "SDS": -2.0, "QID": -2.0, "SQQQ": -3.0, "SPXU": -3.0,
    "SOXS": -3.0, "FAZ": -3.0, "ERY": -3.0, "TZA": -3.0,
    "BITI": -1.0, "SARK": -1.0,
}
# Broad, liquid, -1x S&P inverse ETF — the single instrument the day sleeve uses
# to neutralize its net beta (one target position, never an accumulating pile).
DAY_HEDGE_SYMBOL = "SH"
DAY_HEDGE_BETA = -1.0

# Asset-class default betas for names we don't have a specific number for.
_ASSET_BETA = {"crypto": 1.5, "equity": 1.0, "metals": 0.3, "cash": 0.0, "bond": 0.05}

# Safe / near-zero-beta sleeves (treasuries, T-bills, broad bond funds).
_BOND_LIKE = {
    "SGOV", "BIL", "SHV", "SHY", "GOVT", "IEF", "IEI", "TLT", "TLH",
    "VGSH", "VGIT", "VGLT", "BND", "AGG", "BNDX", "USFR", "TFLO", "GIGB", "XFIV",
}
# Symbol -> coarse sector, for concentration. Derived from the hedge map (a name
# hedged by SOXS is a semi, by FAZ a financial, ...) plus a few explicit adds.
_HEDGE_SECTOR = {"SOXS": "semis", "PSQ": "megacap-tech", "FAZ": "financials",
                 "ERY": "energy", "BITI": "crypto"}
_SECTOR_EXTRA = {
    "SPY": "index", "QQQ": "index", "IWM": "index", "DIA": "index",
    "GLD": "metals", "IAU": "metals", "SLV": "metals",
    "BTC/USD": "crypto", "ETH/USD": "crypto", "BTCUSD": "crypto", "ETHUSD": "crypto",
    "XLV": "healthcare", "XLP": "staples", "XLRE": "real-estate", "XLF": "financials",
    "XLE": "energy", "XLI": "industrials", "XLU": "utilities", "XLK": "tech",
}


def _is_crypto(symbol: str) -> bool:
    s = (symbol or "").upper()
    return "/USD" in s or s.endswith("USD") and s[:-3] in {"BTC", "ETH", "SOL", "DOGE", "LTC"}


def asset_class_for(symbol: str) -> str:
    n = norm_symbol(symbol)
    if _is_crypto(symbol):
        return "crypto"
    if n in {norm_symbol(b) for b in _BOND_LIKE}:
        return "bond"
    if n in {"GLD", "IAU", "SLV", "PSLV", "SGOL"}:
        return "metals"
    return "equity"


def beta_for(symbol: str, asset_class: str | None = None) -> float:
    """Effective beta-to-SPY: negative for inverse ETFs, ~0 for bonds/cash,
    per-name where the hedge map knows it, else an asset-class default."""
    n = norm_symbol(symbol)
    if n in {norm_symbol(k) for k in _INVERSE_BETA}:
        # match on normalized key
        for k, v in _INVERSE_BETA.items():
            if norm_symbol(k) == n:
                return v
    ac = asset_class or asset_class_for(symbol)
    if ac == "bond":
        return _ASSET_BETA["bond"]
    if symbol.upper() in DEFAULT_HEDGES:
        # the hedge map's first beta is the name's sensitivity to its sector hedge
        return DEFAULT_HEDGES[symbol.upper()][1]
    return _ASSET_BETA.get(ac, 1.0)


def sector_for(symbol: str) -> str:
    n = norm_symbol(symbol)
    for k, v in _SECTOR_EXTRA.items():
        if norm_symbol(k) == n:
            return v
    if n in {norm_symbol(b) for b in _BOND_LIKE}:
        return "bonds"
    h = DEFAULT_HEDGES.get(symbol.upper())
    if h:
        return _HEDGE_SECTOR.get(h[0], "equity")
    return "other"


@dataclass
class MarketContext:
    regime: str = "unknown"
    composite_score: float | None = None
    stress_on: bool = False
    dealer_gamma: str = "unknown"          # short | long | unknown
    vol_pctile: float | None = None
    vol_regime: str | None = None          # normal | stress (vol overlay)
    suggested_exposure: float | None = None
    breadth_pct: float | None = None
    strategist_favor: list[str] = field(default_factory=list)
    strategist_avoid: list[str] = field(default_factory=list)
    risk_on: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def build_market_context(duck: DuckStore, *, vol_signal: dict | None = None,
                         strategist: dict | None = None) -> MarketContext:
    ctx = MarketContext()
    notes: list[str] = []

    row = duck.fetchone("SELECT regime, score FROM macro_composite ORDER BY ts DESC LIMIT 1")
    if row and row[0]:
        ctx.regime = row[0]
        ctx.composite_score = float(row[1]) if row[1] is not None else None
        notes.append(f"regime {row[0]}" + (f" (composite {row[1]:+})" if row[1] is not None else ""))
    ctx.risk_on = ctx.regime not in {"risk-off", "stress"}

    row = duck.fetchone(
        "SELECT detail FROM corr_snapshots WHERE card_id = '_meta' ORDER BY ts DESC LIMIT 1")
    if row and row[0]:
        try:
            import json
            ctx.stress_on = bool(json.loads(row[0]).get("stress", {}).get("on"))
        except (ValueError, TypeError):
            ctx.stress_on = False
        if ctx.stress_on:
            notes.append("stress radar LIT")

    row = duck.fetchone(
        "SELECT spot, flip FROM gex_snapshots WHERE symbol = '_SPX' ORDER BY ts DESC LIMIT 1")
    if row and row[0] is not None and row[1] is not None:
        ctx.dealer_gamma = "short" if float(row[0]) < float(row[1]) else "long"
        if ctx.dealer_gamma == "short":
            notes.append("dealers short gamma — moves amplified")

    if vol_signal:
        ctx.vol_pctile = vol_signal.get("vol_percentile")
        ctx.vol_regime = vol_signal.get("regime")
        ctx.suggested_exposure = vol_signal.get("suggested_exposure")
        if ctx.vol_pctile is not None:
            notes.append(f"vol {ctx.vol_pctile:.0f}th pctile ({ctx.vol_regime})")

    row = duck.fetchone(
        "SELECT value FROM ts_macro WHERE series_id = ? ORDER BY ts DESC LIMIT 1", [BREADTH_SERIES])
    if row and row[0] is not None:
        ctx.breadth_pct = float(row[0])
        notes.append(f"breadth {row[0]:.0f}% > 200dma")

    if strategist:
        tilt = strategist.get("equity_tilt") or {}
        ctx.strategist_favor = list(tilt.get("favor") or [])[:5]
        ctx.strategist_avoid = list(tilt.get("avoid") or [])[:5]
        if ctx.strategist_favor:
            notes.append("strategist favors " + ", ".join(ctx.strategist_favor[:3]))

    ctx.notes = notes
    return ctx


@dataclass
class PortfolioState:
    equity: float = 0.0
    cash: float = 0.0
    gross_long: float = 0.0
    gross_short: float = 0.0          # inverse-ETF notional (held long, short exposure)
    net_beta_dollars: float = 0.0     # sum(market_value * beta) — true market exposure
    net_beta_pct: float = 0.0         # net beta $ as % of equity
    sector_exposure: dict[str, float] = field(default_factory=dict)
    swing_value: float = 0.0
    day_value: float = 0.0            # gross day-sleeve exposure (incl. hedges)
    day_budget: float = 0.0
    day_remaining: float = 0.0
    positions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def build_portfolio_state(
    account: dict,
    positions: list[dict],
    sqlite: SqliteStore,
    *,
    day_budget: float,
) -> PortfolioState:
    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    st = PortfolioState()
    st.equity = f(account.get("equity"))
    st.cash = f(account.get("cash"))
    st.day_budget = float(day_budget)

    day_hold = sleeve_holdings(sqlite, "day")
    swing_hold = sleeve_holdings(sqlite, "swing")
    price = {norm_symbol(p.get("symbol", "")): (f(p.get("current_price"))
             or (f(p.get("market_value")) / f(p.get("qty")) if f(p.get("qty")) else 0.0))
             for p in positions}

    sector: dict[str, float] = {}
    norm_pos = []
    for p in positions:
        sym = p.get("symbol", "")
        n = norm_symbol(sym)
        mv = f(p.get("market_value"))
        ac = asset_class_for(sym)
        b = beta_for(sym, ac)
        st.net_beta_dollars += mv * b
        if b < 0:
            st.gross_short += mv
        else:
            st.gross_long += mv
        sec = sector_for(sym)
        sector[sec] = sector.get(sec, 0.0) + mv
        norm_pos.append({"symbol": sym, "qty": f(p.get("qty")), "market_value": round(mv, 2),
                         "beta": b, "sector": sec, "asset_class": ac,
                         "unrealized_pl": f(p.get("unrealized_pl"))})

    # Sleeve value is capped at the broker's ACTUAL position qty: the local
    # ledger can over-count when bracket take-profit/stop child legs fill on the
    # broker but never get recorded (the desync that phantom-inflated day_value
    # and froze the day budget). Bounding by broker truth makes the budget immune
    # to that — a position the broker no longer holds contributes $0.
    broker_qty = {norm_symbol(p.get("symbol", "")): f(p.get("qty")) for p in positions}

    def _sleeve_value(hold: dict[str, float]) -> float:
        return round(sum(min(q, broker_qty.get(n, 0.0)) * (price.get(n) or 0.0)
                         for n, q in hold.items()), 2)

    st.sector_exposure = {k: round(v, 2) for k, v in sorted(sector.items(), key=lambda kv: -abs(kv[1]))}
    st.net_beta_pct = round(st.net_beta_dollars / st.equity * 100.0, 1) if st.equity else 0.0
    st.day_value = _sleeve_value(day_hold)
    st.swing_value = _sleeve_value(swing_hold)
    st.day_remaining = round(max(0.0, st.day_budget - st.day_value), 2)
    st.gross_long = round(st.gross_long, 2)
    st.gross_short = round(st.gross_short, 2)
    st.net_beta_dollars = round(st.net_beta_dollars, 2)
    st.positions = norm_pos
    return st
