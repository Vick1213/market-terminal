"""The 12 intermarket rule-cards (PLAN §4) — pure declarative specs.

A leg is addressed by a namespaced key:
  * ``P:<symbol>``   — daily close from ts_price (yfinance cache-first)
  * ``M:<series>``   — ts_macro series (FRED / CBOE)
  * ``D:<name>``     — derived in the engine (ratios, net liquidity, VIX term)

``ret`` picks the transform used for correlation: "log" for price-like
series (log-returns per PLAN §4 hygiene), "diff" for rates/spreads where a
log of a near-zero or negative level is meaningless.

The static ``normal`` / ``rationale`` / ``breaks_when`` strings come straight
from the PLAN table — the plain-English layer the UI shows per card (Qwen3
"why is it broken" commentary stays deferred off the hot path; the z-score is
the source of truth either way).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Every price leg the cookbook needs, ensured cache-first each run
# (symbol -> asset_class, in ts_price/yahoo_symbol() terms). XAU/XAG map to
# COMEX futures, BTC/USD to BTC-USD inside the existing prices module.
PRICE_LEGS: dict[str, str] = {
    "XAU": "metal",
    "XAG": "metal",
    "BTC/USD": "crypto",
    "^GSPC": "index",
    "^NDX": "index",
    "HG=F": "future",
    "USDJPY=X": "fx",
    "DX-Y.NYB": "index",
}

# ts_macro series the engine loads (besides prices).
MACRO_SERIES = [
    "DGS10", "DFII10", "T10YIE",
    "BAMLH0A0HYM2",
    "T10Y2Y", "T10Y3M",
    "VIXCLS", "VIX", "VIX3M",
    "DTWEXBGS", "DCOILWTICO",
    "WALCL", "RRPONTSYD", "WTREGEN",
]


@dataclass(frozen=True)
class Leg:
    key: str           # namespaced series key (see module docstring)
    label: str
    ret: str = "log"   # "log" | "diff"


@dataclass(frozen=True)
class CardSpec:
    id: str
    num: int
    title: str
    mode: str                      # corr | ratio | curve | term
    legs: tuple[Leg, ...]
    expected_sign: int = 0         # textbook sign of the correlation
    lead_lag: bool = False         # run the ±20d cross-correlation scan
    leader: str = ""               # which leg label is expected to lead
    weekly: bool = False           # resample weekly + scan 0..8wk lags (net-liq)
    secondary: tuple[Leg, Leg] | None = None  # extra corr shown in the detail
    normal: str = ""
    rationale: str = ""
    breaks_when: str = ""
    extra_flags: tuple[str, ...] = field(default=())  # engine sub-flag hooks


CARDS: list[CardSpec] = [
    CardSpec(
        id="gold_btc", num=1, title="Gold vs BTC", mode="corr",
        legs=(Leg("P:XAU", "Gold"), Leg("P:BTC/USD", "BTC")),
        expected_sign=+1,
        normal="weak + (“hard money”)",
        rationale="Both inflation/debasement hedges.",
        breaks_when="CB-driven gold bull while BTC trades as a risk asset.",
    ),
    CardSpec(
        id="coppergold_10y", num=2, title="Copper/Gold vs 10y yield", mode="corr",
        legs=(Leg("D:COPPER_GOLD", "Copper/Gold"), Leg("M:DGS10", "10y yield", ret="diff")),
        expected_sign=+1, lead_lag=True, leader="Copper/Gold",
        normal="+ (~0.85 hist), copper/gold leads",
        rationale="Growth proxy → yields.",
        breaks_when="Gold inflated by CB buying + China copper slump; yields up on deficits, not growth.",
    ),
    CardSpec(
        id="btc_ndx", num=3, title="BTC vs Nasdaq", mode="corr",
        legs=(Leg("P:BTC/USD", "BTC"), Leg("P:^NDX", "Nasdaq 100")),
        expected_sign=+1,
        normal="+ (risk asset)",
        rationale="Same institutional rebalancers.",
        breaks_when="Sustained 30d corr < 0.20 over 60d = decouple.",
    ),
    CardSpec(
        id="usdjpy_risk", num=4, title="USDJPY vs risk", mode="corr",
        legs=(Leg("P:USDJPY=X", "USDJPY"), Leg("P:^NDX", "Nasdaq 100")),
        expected_sign=+1, lead_lag=True, leader="USDJPY",
        secondary=(Leg("P:USDJPY=X", "USDJPY"), Leg("M:VIXCLS", "VIX", ret="diff")),
        normal="+0.78 to Nasdaq, −0.92 to VIX; USDJPY leads",
        rationale="Yen carry funds risk.",
        breaks_when="Sharp yen RALLY → carry unwind → VIX spike (Aug-2024 replay).",
    ),
    CardSpec(
        id="hy_spx", num=5, title="HY spreads vs equities", mode="corr",
        legs=(Leg("M:BAMLH0A0HYM2", "HY OAS", ret="diff"), Leg("P:^GSPC", "S&P 500")),
        expected_sign=-1, extra_flags=("credit_divergence",),
        normal="− (tight spreads = risk-on); credit leads at turns",
        rationale="Credit prices default risk.",
        breaks_when="Spreads WIDEN while SPX still rising = warning.",
    ),
    CardSpec(
        id="curve_2s10s", num=6, title="2s10s curve", mode="curve",
        legs=(Leg("M:T10Y2Y", "2s10s", ret="diff"), Leg("M:T10Y3M", "3m10y", ret="diff")),
        normal="inversion → recession; un-inversion = imminent",
        rationale="Term premium / growth expectations.",
        breaks_when="Bull-steepener after a long inversion = late-cycle warning.",
    ),
    CardSpec(
        id="gold_silver", num=7, title="Gold/Silver ratio", mode="ratio",
        legs=(Leg("D:GOLD_SILVER", "Gold/Silver"),),
        normal="risk-off thermometer",
        rationale="Silver = industrial + precious.",
        breaks_when=">90–100:1 = stress (1991 / Mar-2020).",
    ),
    CardSpec(
        id="netliq_btc", num=8, title="Net liquidity vs BTC", mode="corr",
        legs=(Leg("D:NET_LIQUIDITY", "Net liquidity", ret="diff"), Leg("P:BTC/USD", "BTC")),
        expected_sign=+1, weekly=True, leader="Net liquidity",
        normal="+ with ~5–6 wk lag; liquidity leads",
        rationale="CB liquidity → risk assets (WALCL − RRP − TGA).",
        breaks_when="QT / draining liquidity, reserve cliff.",
    ),
    CardSpec(
        id="real_gold", num=9, title="Real yields vs gold", mode="corr",
        legs=(Leg("M:DFII10", "10y real yield", ret="diff"), Leg("P:XAU", "Gold")),
        expected_sign=-1,
        normal="− (rising real yields = headwind)",
        rationale="Opportunity cost of zero-yield gold.",
        breaks_when="Gold decouples on CB buying (the current regime).",
    ),
    CardSpec(
        id="usd_commod", num=10, title="DXY vs gold", mode="corr",
        legs=(Leg("P:DX-Y.NYB", "DXY"), Leg("P:XAU", "Gold")),
        expected_sign=-1,
        secondary=(Leg("M:DTWEXBGS", "Broad USD"), Leg("M:DCOILWTICO", "WTI crude")),
        normal="− (commodities priced in USD)",
        rationale="True ICE DXY; FRED DTWEXBGS is the broad index (≠ DXY).",
        breaks_when="Both gold and USD bid on safe-haven flows.",
    ),
    CardSpec(
        id="vix_term", num=11, title="VIX term structure", mode="term",
        legs=(Leg("D:VIX_TERM", "VIX3M/VIX"),),
        normal=">1 calm / <1 stress; leads the composite",
        rationale="Vol risk premium.",
        breaks_when="Crosses < 1.0 = early risk-off (backwardation).",
    ),
    CardSpec(
        id="oil_breakeven", num=12, title="Oil vs breakevens", mode="corr",
        legs=(Leg("M:DCOILWTICO", "WTI crude"), Leg("M:T10YIE", "10y breakeven", ret="diff")),
        expected_sign=+1,
        normal="+ (energy → inflation expectations)",
        rationale="Oil feeds inflation expectations.",
        breaks_when="Supply-shock spikes break it.",
    ),
]

CARDS_BY_ID = {c.id: c for c in CARDS}

# 90d-corr heatmap legs (label -> (key, ret)).
HEATMAP_LEGS: list[tuple[str, str, str]] = [
    ("SPX", "P:^GSPC", "log"),
    ("NDX", "P:^NDX", "log"),
    ("BTC", "P:BTC/USD", "log"),
    ("Gold", "P:XAU", "log"),
    ("Silver", "P:XAG", "log"),
    ("Copper", "P:HG=F", "log"),
    ("USDJPY", "P:USDJPY=X", "log"),
    ("DXY", "P:DX-Y.NYB", "log"),
    ("10y", "M:DGS10", "diff"),
    ("HY OAS", "M:BAMLH0A0HYM2", "diff"),
    ("Oil", "M:DCOILWTICO", "log"),
]
