"""Strategist — cross-panel synthesis into a suggested allocation (Phase 9 §5).

Reads ONLY data the other pipelines already store (no network on this path):
macro composite + regime, net-liq trend, corr cookbook statuses + stress
radar, RRG quadrants, COT extremes, GEX regime, put/call extremes, 48h news
pulse, Event Horizon proximity, crypto large-print flow, benchmark
seasonality, retail spikes, insider clusters (watchlist + market-wide scan),
whale new positions, congress trades, short-volume pressure, earnings
proximity and Lazy-Prices filings diffs.

Three layers:
  a. Deterministic core — a transparent rules table maps regime + signals to
     a diversified allocation across the asset classes the terminal already
     covers (equities w/ RRG sector tilt, gold/silver, BTC/ETH, cash/short
     duration). Every tilt carries the signal that drove it and a reasoning
     string, and every input signal is echoed back with its freshness so the
     UI can show "why" — this is signal synthesis, NOT financial advice.
  b. Picks layer — each sleeve is split into individual holdings: the equity
     sleeve into RRG-weighted sector ETFs plus single-name picks scored across
     every symbol-level signal stored (insider clusters, Senate PTR flow, 13F
     whale adds, 7d FinBERT news sentiment, retail froth, relative tape);
     metals into GLD/SLV by the gold/silver ratio + COT; crypto into BTC/ETH
     by 3m relative momentum + COT; cash into T-bill ETFs. Every holding
     carries the evidence lines that produced it.
  c. Optional LLM narrative (edge/llm.py, deterministic note builder as the
     fallback) turning the rules output into 3-5 short strategy notes.

Snapshots persist daily (one row/day, re-runs replace) so suggestions are
reviewable in hindsight. The compute path is blocking (DuckDB + pandas) and
is dispatched via run_in_executor by ``StrategistService.run``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.edge.cot import cot_summary
from app.edge.llm import LlmClient
from app.edge.posture import compute_posture, name_lifecycle
from app.edge.rotation import SECTOR_NAMES, compute_rrg, compute_seasonality
from app.edge.whales import whale_new_positions
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.edge.strategist")

STRATEGIST_TOPIC = "strategist"

DISCLAIMER = (
    "Signal synthesis from stored terminal data — NOT financial advice. "
    "Inputs lag (13F up to 45d, COT weekly) and rules are mechanical. "
    "Single-name picks are signal overlaps, not recommendations."
)

BUCKETS = [
    ("equities", "Equities (sector-tilted)"),
    ("metals", "Gold / Silver"),
    ("crypto", "BTC / ETH"),
    ("cash", "Cash / short duration"),
]

# Regime -> base allocation (%). The starting point every tilt adjusts from.
BASE_ALLOCATION: dict[str, dict[str, float]] = {
    "risk-on":  {"equities": 45, "metals": 10, "crypto": 10, "cash": 35},
    "neutral":  {"equities": 35, "metals": 15, "crypto": 5,  "cash": 45},
    "risk-off": {"equities": 20, "metals": 20, "crypto": 0,  "cash": 60},
    "stress":   {"equities": 10, "metals": 25, "crypto": 0,  "cash": 65},
    "unknown":  {"equities": 25, "metals": 15, "crypto": 5,  "cash": 55},
}

# COT market label -> allocation bucket (exact labels as the COT pipeline
# stores them — substring matching false-positives on e.g. "NOTES").
_COT_BUCKET = {
    "GOLD": "metals", "SILVER": "metals",
    "BITCOIN": "crypto", "ETHER": "crypto",
    "E-MINI S&P": "equities", "NASDAQ": "equities",
}

_NOTES_PROMPT = """You are the strategist for a personal market terminal.
Below is a machine-generated allocation suggestion (JSON) built from stored
signals. Write 3-5 short strategy notes in markdown (one "- " bullet each,
<=25 words per note) connecting the signals to positioning, e.g.
"gamma short + risk-off: size down, expect range expansion". Name specific
holdings (sector ETFs, single names, GLD/SLV or BTC/ETH splits) where the
buckets' holdings suggest them. Only use facts from the JSON, no invented
numbers, no disclaimers, no preamble.

DATA:
{data}
"""


def _age_days(ts) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts[:19])
        except ValueError:
            return None
    return (datetime.now(timezone.utc).replace(tzinfo=None) - ts).days


def _signal(key: str, label: str, value, detail: str,
            ts=None, max_age_days: int = 7) -> dict:
    age = _age_days(ts)
    return {
        "key": key, "label": label, "value": value, "detail": detail,
        "asof": str(ts)[:10] if ts is not None else None,
        "stale": age is None or age > max_age_days,
    }


# ------------------------------------------------------------- picks layer

# 13F holdings carry CUSIPs + issuer names, not tickers. Resolution is
# best-effort: exact match against watchlist display names first, then this
# static large-cap map (famous-fund top-10s are overwhelmingly mega-caps);
# unresolved issuers simply don't contribute to single-name scores.
_ISSUER_TICKERS = {
    "APPLE": "AAPL", "MICROSOFT": "MSFT", "AMAZON": "AMZN",
    "ALPHABET": "GOOGL", "META PLATFORMS": "META", "NVIDIA": "NVDA",
    "TESLA": "TSLA", "BERKSHIRE HATHAWAY": "BRK-B", "COCA COLA": "KO",
    "BANK OF AMERICA": "BAC", "AMERICAN EXPRESS": "AXP", "CHEVRON": "CVX",
    "OCCIDENTAL PETROLEUM": "OXY", "KRAFT HEINZ": "KHC", "MOODYS": "MCO",
    "VISA": "V", "MASTERCARD": "MA", "UNITEDHEALTH GROUP": "UNH",
    "JPMORGAN CHASE": "JPM", "ELI LILLY": "LLY", "BROADCOM": "AVGO",
    "NETFLIX": "NFLX", "ADOBE": "ADBE", "SALESFORCE": "CRM",
    "ADVANCED MICRO DEVICES": "AMD", "INTEL": "INTC", "QUALCOMM": "QCOM",
    "EXXON MOBIL": "XOM", "JOHNSON JOHNSON": "JNJ", "PROCTER GAMBLE": "PG",
    "WALMART": "WMT", "HOME DEPOT": "HD", "WALT DISNEY": "DIS",
    "PAYPAL": "PYPL", "UBER TECHNOLOGIES": "UBER",
    "PALANTIR TECHNOLOGIES": "PLTR", "TAIWAN SEMICONDUCTOR": "TSM",
    "ALIBABA GROUP": "BABA", "MICRON TECHNOLOGY": "MU", "CHUBB": "CB",
    "CITIGROUP": "C", "WELLS FARGO": "WFC", "GOLDMAN SACHS GROUP": "GS",
    "PEPSICO": "PEP", "COSTCO WHOLESALE": "COST", "DOMINOS PIZZA": "DPZ",
    "CONSTELLATION BRANDS": "STZ", "HP": "HPQ", "VERISIGN": "VRSN",
}

# Legal-form noise stripped before issuer matching (INC, CORP, CL A, DEL ...).
_ISSUER_NOISE_RE = re.compile(
    r"\b(INC|CORP|CO|COM|LTD|PLC|SA|NV|HLDGS?|HOLDINGS?|GRP|CL|CLASS|SER|"
    r"SHS|ADR|ADS|NEW|DEL|COMPANY|COS|TR|[A-C])\b"
)

# Symbols eligible as single-name picks (skips BTC/USD, _SPX, spot codes ...).
_TICKER_OK = re.compile(r"^[A-Z]{1,6}([.\-][A-Z]{1,2})?$")

# Index/asset ETFs the sleeves already cover — never single-name picks.
_ETF_EXCLUDE = {"SPY", "QQQ", "DIA", "IWM", "GLD", "SLV", "TLT", "SGOV", "BIL"}


def _norm_issuer(name: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    s = _ISSUER_NOISE_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _edgar_names(sqlite: SqliteStore) -> dict[str, str]:
    """Normalized EDGAR company title -> ticker, from the cache the news
    pipeline maintains (app_meta 'edgar_cik_map'). Empty until the first
    EDGAR ingest has run — the static map below covers the gap."""
    row = sqlite.fetchone("SELECT value FROM app_meta WHERE key = 'edgar_cik_map'")
    if not row:
        return {}
    try:
        names = json.loads(row["value"]).get("names") or {}
    except ValueError:
        return {}
    out: dict[str, str] = {}
    for title, ticker in names.items():
        n = _norm_issuer(title)
        if n and (n not in out or len(ticker) < len(out[n])):
            out[n] = ticker
    return out


def _resolve_issuer(issuer: str, watch_names: dict[str, str],
                    edgar_names: dict[str, str] | None = None) -> str | None:
    n = _norm_issuer(issuer)
    if not n:
        return None
    edgar_names = edgar_names or {}
    hit = watch_names.get(n) or _ISSUER_TICKERS.get(n) or edgar_names.get(n)
    if hit:
        return hit
    for key, sym in _ISSUER_TICKERS.items():
        if n.startswith(key + " "):
            return sym
    # 13F issuer names are often truncated ("TAIWAN SEMICONDUCTOR MANUFAC");
    # accept a prefix match against EDGAR titles only when it is unambiguous.
    if len(n) >= 8:
        hits = {sym for title, sym in edgar_names.items()
                if title.startswith(n)}
        if len(hits) == 1:
            return hits.pop()
    return None


def _price_series(duck: DuckStore, symbol: str, limit: int = 260) -> list[tuple]:
    """Daily (ts, close) ascending — tries the raw symbol then the yahoo
    dash form (watchlist crypto is stored as BTC/USD, yahoo as BTC-USD)."""
    for sym in dict.fromkeys([symbol, symbol.replace("/", "-")]):
        rows = duck.fetchall(
            "SELECT ts, close FROM ts_price WHERE source = 'yahoo' AND symbol = ? "
            "AND close IS NOT NULL ORDER BY ts DESC LIMIT ?",
            [sym, limit],
        )
        if rows:
            return rows[::-1]
    return []


def _pct_return(duck: DuckStore, symbol: str, bars: int) -> float | None:
    rows = _price_series(duck, symbol, bars + 1)
    if len(rows) < bars + 1:
        return None
    first, last = float(rows[0][1]), float(rows[-1][1])
    return (last / first - 1) * 100 if first else None


def _equity_stock_picks(
    duck: DuckStore, sqlite: SqliteStore, benchmark: str, now: datetime,
    max_picks: int = 5, factor_leaders: list[dict] | None = None,
) -> list[dict]:
    """Score single names across every symbol-level signal the terminal
    stores; only names clearing the conviction bar survive, each carrying
    the evidence lines that produced its score. Blocking."""
    cands: dict[str, dict] = {}

    def add(sym: str | None, pts: float, why: str) -> None:
        sym = (sym or "").upper().strip()
        if not sym or not _TICKER_OK.match(sym):
            return
        c = cands.setdefault(sym, {"symbol": sym, "score": 0.0, "evidence": []})
        c["score"] += pts
        c["evidence"].append(why)

    # Fundamental factor screen (value/quality/small composite from EDGAR XBRL).
    # A mild descriptive + — the composite has no deflation-cleared alpha (size
    # carries it, fails DSR), so it nudges conviction, never drives it alone.
    for r in factor_leaders or []:
        add(r.get("symbol"), 1.0,
            f"screens cheap/quality/small (factor composite {r.get('score', 0):+.1f})")

    # Insider Form 4 open-market buys (14d) — the strongest informed signal.
    for sym, buyers, value, ceo in duck.fetchall(
        "SELECT upper(symbol), count(DISTINCT insider_name), sum(value), "
        "max(CASE WHEN is_ceo_cfo THEN 1 ELSE 0 END) "
        "FROM insider_trades WHERE filed_at >= ? GROUP BY upper(symbol)",
        [now - timedelta(days=14)],
    ):
        pts = 3.0 if (buyers or 0) >= 2 else 1.5
        why = f"{buyers} insider open-market buy(s) in 14d"
        if value:
            why += f", ~${value / 1e6:.2f}M"
        if ceo:
            pts += 1.0
            why += ", incl. CEO/CFO"
        add(sym, pts, why)

    # Senate PTR flow (30d), net of sells.
    for sym, buys, sells, amt in duck.fetchall(
        "SELECT upper(ticker), "
        "sum(CASE WHEN side = 'buy' THEN 1 ELSE 0 END), "
        "sum(CASE WHEN side = 'sell' THEN 1 ELSE 0 END), "
        "sum(CASE WHEN side = 'buy' THEN coalesce(amount_min, 0) ELSE 0 END) "
        "FROM congress_trades WHERE ticker IS NOT NULL AND filed_at >= ? "
        "GROUP BY upper(ticker)",
        [now - timedelta(days=30)],
    ):
        net = int(buys or 0) - int(sells or 0)
        if net > 0:
            add(sym, min(2.0, float(net)),
                f"{buys} Senate PTR buy(s) vs {sells or 0} sell(s) in 30d "
                f"(>= ${amt:,.0f} disclosed)")
        elif net <= -2:
            add(sym, -1.0, f"{sells} Senate PTR sell(s) vs {buys or 0} buy(s) in 30d")

    # 13F whale new top-10 positions (120d window — filings lag up to 45d).
    watch_names: dict[str, str] = {}
    for r in sqlite.fetchall(
        "SELECT symbol, display_name FROM watchlist WHERE asset_class = 'equity'"
    ):
        n = _norm_issuer(r["display_name"] or "")
        if n:
            watch_names[n] = r["symbol"]
    edgar_names = _edgar_names(sqlite)
    for w in whale_new_positions(duck, days=120, top_rank=10):
        sym = _resolve_issuer(w["issuer"] or "", watch_names, edgar_names)
        if sym:
            add(sym, 2.0, f"{w['fund']} opened a top-10 13F position ({w['issuer']})")

    # 7d aggregated FinBERT news sentiment, >=3 scored stories.
    for sym, avg, n_items in duck.fetchall(
        "SELECT upper(symbol), avg(score), count(*) FROM news_items "
        "WHERE symbol IS NOT NULL AND score IS NOT NULL AND published >= ? "
        "GROUP BY upper(symbol) HAVING count(*) >= 3",
        [now - timedelta(days=7)],
    ):
        if avg is None:
            continue
        if avg >= 0.2:
            add(sym, min(2.0, float(avg) * 4),
                f"news sentiment {avg:+.2f} across {n_items} stories (7d)")
        elif avg <= -0.2:
            add(sym, max(-2.0, float(avg) * 4),
                f"news sentiment {avg:+.2f} across {n_items} stories (7d)")

    # Lazy Prices: a recent risk-factor rewrite is negative evidence (and a
    # quiet re-file is mildly reassuring for names already in play).
    for sym, sim, form in duck.fetchall(
        "SELECT upper(symbol), similarity, form FROM filings_diff "
        "WHERE similarity IS NOT NULL AND filed_at >= ?",
        [now - timedelta(days=120)],
    ):
        if sim < 0.75:
            add(sym, -1.5, f"{form} risk factors rewritten "
                           f"({sim:.0%} similar) — Lazy Prices red flag")
        elif sim > 0.95 and sym in cands:
            add(sym, +0.5, f"{form} risk factors unchanged ({sim:.0%}) — "
                           "no new skeletons disclosed")

    # Retail mention spikes read as froth, not conviction.
    for (sym,) in duck.fetchall(
        "SELECT upper(symbol) FROM ts_retail WHERE source = 'apewisdom' "
        "AND ts = (SELECT max(ts) FROM ts_retail WHERE source = 'apewisdom') "
        "AND mentions_prev >= 20 AND mentions >= 3 * mentions_prev"
    ):
        if sym in cands:
            add(sym, -0.5, "retail mention spike 3x+ — crowd already piling in")

    for sym in _ETF_EXCLUDE | set(SECTOR_NAMES) | {benchmark}:
        cands.pop(sym, None)

    # Tape confirmation on the survivors only (a handful of price lookups).
    bench_r = _pct_return(duck, benchmark, 21)
    if bench_r is not None:
        for sym in list(cands):
            r = _pct_return(duck, sym, 21)
            if r is None:
                continue
            rel = r - bench_r
            if rel >= 3:
                add(sym, 0.5, f"{rel:+.1f}pp vs {benchmark} over 1m — tape confirms")
            elif rel <= -5:
                add(sym, -1.0, f"{rel:+.1f}pp vs {benchmark} over 1m — tape disagrees")

    # Short-volume pressure + earnings proximity, survivors only.
    for sym in list(cands):
        row = duck.fetchone(
            "SELECT short_volume / nullif(total_volume, 0), ts FROM ts_short_vol "
            "WHERE upper(symbol) = ? ORDER BY ts DESC LIMIT 1", [sym],
        )
        if row and row[0] is not None and _age_days(row[1]) is not None \
                and _age_days(row[1]) <= 5 and float(row[0]) >= 0.6:
            add(sym, -0.5, f"short volume {float(row[0]):.0%} of trading — "
                           "heavy shorting pressure")
        ev = duck.fetchone(
            "SELECT ts FROM events WHERE kind = 'earnings' AND upper(symbol) = ? "
            "AND ts BETWEEN ? AND ?",
            [sym, now, now + timedelta(days=7)],
        )
        if ev:
            add(sym, 0.0, f"earnings {str(ev[0])[:10]} — binary event inside "
                          "the holding window")

    picks = [c for c in cands.values() if c["score"] >= 1.5]
    picks.sort(key=lambda c: c["score"], reverse=True)
    return picks[:max_picks]


def _sector_sleeve(rrg: dict) -> list[dict]:
    """Split the ETF part of the equity sleeve across leading/improving RRG
    sectors, weighted by combined RS strength. No favored sectors -> broad."""
    favored = [s for s in rrg.get("sectors", [])
               if s["quadrant"] in ("leading", "improving")]
    favored.sort(key=lambda s: s["rs_ratio"] + s["rs_momentum"], reverse=True)
    favored = favored[:4]
    if not favored:
        return [{
            "symbol": rrg.get("benchmark", "SPY"), "name": "Broad market",
            "kind": "sector", "share": 100.0,
            "evidence": ["no RRG sector leading/improving — stay broad"],
        }]
    raw = [max(0.5, (s["rs_ratio"] - 100) + (s["rs_momentum"] - 100) + 4)
           for s in favored]
    total = sum(raw)
    return [
        {
            "symbol": s["symbol"], "name": s["name"], "kind": "sector",
            "share": w / total * 100,
            "evidence": [f"RRG {s['quadrant']} (ratio {s['rs_ratio']:.1f}, "
                         f"momentum {s['rs_momentum']:.1f})"],
        }
        for s, w in zip(favored, raw)
    ]


def _metals_split(duck: DuckStore, extremes: list[dict]) -> list[dict]:
    """GLD/SLV inside the metals sleeve: 65/35 base, tilted by the 1y
    gold/silver ratio percentile and COT positioning extremes."""
    gold, silver = 65.0, 35.0
    ev_g = ["core defensive sleeve"]
    ev_s = ["higher-beta metal kicker"]
    slv = {ts: float(c) for ts, c in _price_series(duck, "SLV")}
    ratio = [float(c) / slv[ts] for ts, c in _price_series(duck, "GLD")
             if ts in slv and slv[ts]]
    if len(ratio) >= 60:
        last = ratio[-1]
        pct = 100 * sum(1 for r in ratio if r <= last) / len(ratio)
        if pct >= 80:
            gold, silver = gold - 10, silver + 10
            ev_s.append(f"gold/silver ratio in top {100 - pct:.0f}% of 1y — "
                        "silver historically cheap vs gold")
        elif pct <= 20:
            gold, silver = gold + 10, silver - 10
            ev_g.append(f"gold/silver ratio in bottom {pct:.0f}% of 1y — "
                        "gold cheap vs silver")
    for c in extremes:
        m, idx = str(c["market"]).upper().strip(), c["cot_index"]
        if m not in ("GOLD", "SILVER"):
            continue
        crowded = idx >= 90
        # Crowded metal cedes 5pp to the other; washed-out metal takes 5pp.
        toward_silver = (m == "SILVER") != crowded
        delta = 5.0 if toward_silver else -5.0
        gold, silver = gold - delta, silver + delta
        why = (f"{m.lower()} spec positioning "
               + (f"crowded (COT {idx:.0f})" if crowded
                  else f"washed out (COT {idx:.0f}) — contrarian"))
        (ev_s if toward_silver else ev_g).append(why)
    return [
        {"symbol": "GLD", "name": "Gold", "kind": "asset", "share": gold,
         "evidence": ev_g},
        {"symbol": "SLV", "name": "Silver", "kind": "asset", "share": silver,
         "evidence": ev_s},
    ]


def _crypto_split(duck: DuckStore, extremes: list[dict]) -> list[dict]:
    """BTC/ETH inside the crypto sleeve: 70/30 base, tilted by 3m relative
    momentum and COT positioning extremes."""
    btc, eth = 70.0, 30.0
    ev_b = ["core crypto holding — deepest liquidity"]
    ev_e = ["secondary major"]
    rb = _pct_return(duck, "BTC/USD", 63)
    re_ = _pct_return(duck, "ETH/USD", 63)
    if rb is not None and re_ is not None:
        rel = re_ - rb
        if rel >= 15:
            btc, eth = btc - 10, eth + 10
            ev_e.append(f"ETH {rel:+.0f}pp vs BTC over 3m — rotation toward ETH")
        elif rel <= -15:
            btc, eth = btc + 10, eth - 10
            ev_b.append(f"ETH {rel:+.0f}pp vs BTC over 3m — BTC leadership intact")
    for c in extremes:
        m, idx = str(c["market"]).upper().strip(), c["cot_index"]
        if m not in ("BITCOIN", "ETHER"):
            continue
        crowded = idx >= 90
        toward_eth = (m == "ETHER") != crowded
        delta = 5.0 if toward_eth else -5.0
        btc, eth = btc - delta, eth + delta
        why = (f"{'BTC' if m == 'BITCOIN' else 'ETH'} futures positioning "
               + (f"crowded (COT {idx:.0f})" if crowded
                  else f"washed out (COT {idx:.0f}) — contrarian"))
        (ev_e if toward_eth else ev_b).append(why)
    return [
        {"symbol": "BTC", "name": "Bitcoin", "kind": "asset", "share": btc,
         "evidence": ev_b},
        {"symbol": "ETH", "name": "Ethereum", "kind": "asset", "share": eth,
         "evidence": ev_e},
    ]


def _cash_sleeve() -> list[dict]:
    return [{
        "symbol": "SGOV", "name": "0-3m T-bills", "kind": "asset",
        "share": 100.0,
        "evidence": ["earns the policy rate while parked, no duration risk"],
    }]


def compute_strategist(
    duck: DuckStore, sqlite: SqliteStore, sectors: list[str], benchmark: str
) -> dict:
    """The deterministic core. Blocking — run_in_executor."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    signals: list[dict] = []
    tilts: list[tuple[str, float, str, str]] = []  # (bucket, delta, signal key, reason)

    # --- POSTURE engine (the shared macro dial) ------------------------------
    # One coherent top-down read (monthly tide / weekly trend / regime / breadth
    # / net-liq) the whole book runs on. Computed once here and stapled onto the
    # snapshot so the swing bot and the strategist agree on gross + lifecycle.
    # Defensive: any failure leaves posture=None and the OLD behaviour intact
    # (no gross scaling, no lifecycle tags).
    posture: dict | None = None
    try:
        posture = compute_posture(duck, benchmark=benchmark)
    except Exception as exc:  # missing data / pandas — degrade gracefully
        log.warning("posture engine failed (%s) — snapshot runs un-postured", exc)
        posture = None

    # --- regime / composite ------------------------------------------------
    regime = "unknown"
    row = duck.fetchone(
        "SELECT score, regime, ts FROM macro_composite ORDER BY ts DESC LIMIT 1"
    )
    score = None
    if row:
        score = round(float(row[0]), 1)
        sig = _signal("composite", "Macro composite", score,
                      f"regime {row[1]}", row[2], max_age_days=3)
        signals.append(sig)
        if not sig["stale"] and row[1]:
            regime = row[1]
    else:
        signals.append(_signal("composite", "Macro composite", None, "no data yet"))

    # --- net liquidity trend -------------------------------------------------
    netliq = duck.fetchall(
        "SELECT ts, value FROM ts_macro WHERE series_id = 'NET_LIQUIDITY' "
        "ORDER BY ts DESC LIMIT 21"
    )
    if netliq:
        d20 = (float(netliq[0][1]) - float(netliq[20][1])) if len(netliq) == 21 else None
        sig = _signal("net_liquidity", "Net liquidity (1m Δ)",
                      round(d20) if d20 is not None else None,
                      f"level {float(netliq[0][1]):,.0f} $bn", netliq[0][0])
        signals.append(sig)
        if not sig["stale"] and d20 is not None:
            if d20 >= 50:
                tilts += [("crypto", +3, "net_liquidity", f"liquidity rising {d20:+,.0f} $bn/1m — tailwind for risk, BTC most sensitive"),
                          ("equities", +2, "net_liquidity", f"liquidity rising {d20:+,.0f} $bn/1m"),
                          ("cash", -5, "net_liquidity", "liquidity tailwind — deploy from cash")]
            elif d20 <= -150:
                tilts += [("crypto", -3, "net_liquidity", f"liquidity draining {d20:+,.0f} $bn/1m — BTC most exposed"),
                          ("equities", -2, "net_liquidity", f"liquidity draining {d20:+,.0f} $bn/1m"),
                          ("metals", +1, "net_liquidity", "drain favors defensives"),
                          ("cash", +4, "net_liquidity", "liquidity headwind — raise cash")]
    else:
        signals.append(_signal("net_liquidity", "Net liquidity (1m Δ)", None, "no data yet"))

    # --- corr cookbook + stress radar ---------------------------------------
    row = duck.fetchone(
        "SELECT ts, detail FROM corr_snapshots WHERE card_id = '_meta' "
        "ORDER BY ts DESC LIMIT 1"
    )
    if row and row[1]:
        meta = json.loads(row[1])
        broken = [c.get("title") for c in meta.get("cards", []) if c.get("status") == "broken"]
        stress_on = bool(meta.get("stress", {}).get("on"))
        sig = _signal("cookbook", "Cookbook broken cards", len(broken),
                      ", ".join(broken) or "all relationships holding",
                      row[0], max_age_days=2)
        signals.append(sig)
        if not sig["stale"] and len(broken) >= 3:
            tilts += [("equities", -2, "cookbook", f"{len(broken)} intermarket relationships BROKEN — cross-asset signals unreliable"),
                      ("cash", +2, "cookbook", "multiple correlation breaks — reduce gross exposure")]
        sig = _signal("stress_radar", "Stress radar", "ON" if stress_on else "off",
                      "yen rally + VIX backwardation + gold/silver rising" if stress_on
                      else "no Aug-2024-style confluence", row[0], max_age_days=2)
        signals.append(sig)
        if not sig["stale"] and stress_on:
            tilts += [("equities", -4, "stress_radar", "stress confluence lit — de-risk"),
                      ("metals", +3, "stress_radar", "stress confluence favors gold"),
                      ("cash", +1, "stress_radar", "stress confluence — keep powder dry")]
    else:
        signals.append(_signal("cookbook", "Cookbook broken cards", None, "no data yet"))

    # --- vol-targeted exposure overlay (the deployable risk signal) ----------
    # The one signal with a validated edge: forward vol is forecastable, so we
    # size exposure inversely to the GK-vol forecast. NOT alpha — defensive.
    try:
        from app.ml.vol_overlay import current_signal as _vol_signal
        vs = _vol_signal("SPY", duck=duck)
    except Exception as exc:  # missing OHLC / ml deps — fail soft
        log.warning("vol_overlay signal failed (%s)", exc)
        vs = None
    if vs:
        exp = float(vs["suggested_exposure"])
        sig = _signal("vol_overlay", "Vol-target exposure", f"{exp:.0%}",
                      f"SPY forecast vol {vs['forecast_vol_annual']:.0%} "
                      f"({vs['vol_percentile']:.0%}-ile), target {vs['target_vol']:.0%}",
                      vs["as_of"], max_age_days=3)
        signals.append(sig)
        if not sig["stale"]:
            if exp <= 0.70:
                cut = int(round((1.0 - exp) * 6))  # de-risk proportional to vol
                tilts += [("equities", -cut, "vol_overlay",
                           f"SPY forecast vol {vs['forecast_vol_annual']:.0%} above target — "
                           f"vol-target sizing says hold {exp:.0%} equity, trim into the vol"),
                          ("cash", +cut, "vol_overlay",
                           "elevated-vol regime — vol-target overlay raises cash")]
            elif exp >= 0.99 and float(vs["vol_percentile"]) <= 0.40:
                tilts.append(("equities", +1, "vol_overlay",
                              f"forecast vol calm ({vs['vol_percentile']:.0%}-ile) — "
                              "vol-target allows full size"))
    else:
        signals.append(_signal("vol_overlay", "Vol-target exposure", None,
                               "no SPY OHLC for vol overlay"))

    # --- RRG rotation (sector tilt inside the equity sleeve) -----------------
    rrg = compute_rrg(duck, sectors, benchmark)
    equity_tilt = None
    if rrg.get("sectors"):
        by_q: dict[str, list] = {}
        for s in rrg["sectors"]:
            by_q.setdefault(s["quadrant"], []).append(s)
        favor = sorted(
            by_q.get("leading", []) + by_q.get("improving", []),
            key=lambda s: s["rs_momentum"], reverse=True,
        )
        avoid = sorted(by_q.get("lagging", []), key=lambda s: s["rs_momentum"])
        bench_row = duck.fetchone(
            "SELECT max(ts) FROM ts_price WHERE source = 'yahoo' AND symbol = ?",
            [benchmark],
        )
        bench_ts = bench_row[0] if bench_row else None
        equity_tilt = {
            "benchmark": benchmark,
            "favor": [f"{s['symbol']} ({s['name']})" for s in favor[:4]],
            "avoid": [f"{s['symbol']} ({s['name']})" for s in avoid[:4]],
        }
        defensives = {"XLU", "XLP", "XLV"}
        leading_syms = {s["symbol"] for s in by_q.get("leading", [])}
        detail = (f"leading: {', '.join(sorted(leading_syms)) or '—'}")
        sig = _signal("rrg", "RRG rotation", len(leading_syms), detail, bench_ts)
        signals.append(sig)
        if not sig["stale"] and leading_syms and leading_syms <= defensives:
            tilts += [("equities", -2, "rrg", "only defensive sectors (XLU/XLP/XLV) leading — defensive tape under the surface"),
                      ("cash", +2, "rrg", "defensive leadership — risk appetite is narrow")]
    else:
        signals.append(_signal("rrg", "RRG rotation", None, "no sector history yet"))

    # --- COT extremes (crowding) ---------------------------------------------
    extremes = [c for c in cot_summary(duck)
                if c.get("cot_index") is not None
                and (c["cot_index"] >= 90 or c["cot_index"] <= 10)]
    latest_cot = max((c["report_date"] for c in cot_summary(duck)), default=None)
    sig = _signal("cot", "COT extremes", len(extremes),
                  ", ".join(f"{c['market']} {c['cot_index']:.0f}" for c in extremes)
                  or "no 3y positioning extremes",
                  latest_cot, max_age_days=14)
    signals.append(sig)
    if not sig["stale"]:
        for c in extremes:
            bucket = _COT_BUCKET.get(str(c["market"]).upper().strip())
            if bucket is None:
                continue
            if c["cot_index"] >= 90:
                tilts.append((bucket, -2, "cot",
                              f"{c['market']} spec positioning at 3y max-bullish (COT {c['cot_index']:.0f}) — crowded, unwind risk"))
            else:
                tilts.append((bucket, +2, "cot",
                              f"{c['market']} spec positioning at 3y max-bearish (COT {c['cot_index']:.0f}) — washed out, contrarian"))

    # --- GEX regime -----------------------------------------------------------
    row = duck.fetchone(
        "SELECT ts, spot, flip FROM gex_snapshots WHERE symbol = '_SPX' "
        "ORDER BY ts DESC LIMIT 1"
    )
    if row and row[1] is not None and row[2] is not None:
        spot, flip = float(row[1]), float(row[2])
        short_gamma = spot < flip
        sig = _signal("gex", "SPX dealer gamma",
                      "short" if short_gamma else "long",
                      f"spot {spot:,.0f} vs flip {flip:,.0f}", row[0], max_age_days=2)
        signals.append(sig)
        if not sig["stale"] and short_gamma:
            tilts += [("equities", -3, "gex", "dealers short gamma (spot below flip) — moves amplify, size down"),
                      ("cash", +3, "gex", "short-gamma regime — expect range expansion")]
    else:
        signals.append(_signal("gex", "SPX dealer gamma", None, "no GEX snapshot yet"))

    # --- put/call extreme (contrarian) -----------------------------------------
    # The composite already consumes PC_TOTAL as a slow z-input; this tilt
    # only reacts to EXTREMES — panic hedging is a washout buy signal, a
    # collapsed ratio is complacency.
    pc = duck.fetchall(
        "SELECT ts, value FROM ts_macro WHERE series_id = 'PC_TOTAL' "
        "AND value IS NOT NULL ORDER BY ts DESC LIMIT 252"
    )
    if len(pc) >= 60:
        vals = [float(r[1]) for r in pc]
        mu = sum(vals[1:]) / len(vals[1:])
        sd = (sum((v - mu) ** 2 for v in vals[1:]) / len(vals[1:])) ** 0.5
        pc_z = (vals[0] - mu) / sd if sd else 0.0
        sig = _signal("put_call", "Put/Call (1y z)", round(pc_z, 1),
                      f"total ratio {vals[0]:.2f}", pc[0][0], max_age_days=5)
        signals.append(sig)
        if not sig["stale"]:
            if pc_z >= 2.0:
                tilts += [("equities", +2, "put_call",
                           f"put/call {pc_z:+.1f}σ — panic hedging, contrarian washout"),
                          ("cash", -2, "put_call", "hedging extreme — fear already paid for")]
            elif pc_z <= -2.0:
                tilts += [("equities", -1, "put_call",
                           f"put/call {pc_z:+.1f}σ — nobody hedged, complacency"),
                          ("cash", +1, "put_call", "complacent options market — cheap to wait")]
    else:
        signals.append(_signal("put_call", "Put/Call (1y z)", None, "no PC_TOTAL history yet"))

    # --- 48h news pulse ---------------------------------------------------------
    row = duck.fetchone(
        "SELECT avg(score), count(*), max(published) FROM news_items "
        "WHERE score IS NOT NULL AND confidence >= 0.5 AND published >= ?",
        [now - timedelta(days=2)],
    )
    if row and row[1] and int(row[1]) >= 20:
        pulse, n_items = float(row[0]), int(row[1])
        sig = _signal("news_pulse", "News pulse (48h)", round(pulse, 2),
                      f"{n_items} FinBERT-scored headlines", row[2], max_age_days=2)
        signals.append(sig)
        if not sig["stale"]:
            if pulse <= -0.15:
                tilts += [("equities", -1, "news_pulse",
                           f"48h news flow {pulse:+.2f} across {n_items} headlines — bearish tape reading"),
                          ("cash", +1, "news_pulse", "negative news pulse — no rush to deploy")]
            elif pulse >= 0.15:
                tilts.append(("equities", +1, "news_pulse",
                              f"48h news flow {pulse:+.2f} across {n_items} headlines — bullish tape reading"))
    else:
        signals.append(_signal("news_pulse", "News pulse (48h)", None,
                               "not enough scored headlines"))

    # --- event horizon proximity -------------------------------------------------
    ev = duck.fetchone(
        "SELECT title, ts, kind FROM events WHERE ts >= ? "
        "AND kind IN ('fomc', 'cpi', 'nfp') ORDER BY ts LIMIT 1",
        [datetime(now.year, now.month, now.day)],
    )
    if ev:
        days_to = (ev[1].date() - now.date()).days
        sig = _signal("event_horizon", "Next macro event",
                      f"{str(ev[2]).upper()} in {days_to}d", str(ev[0]),
                      now, max_age_days=1)
        signals.append(sig)
        if days_to <= 2:
            tilts += [("equities", -1, "event_horizon",
                       f"{str(ev[2]).upper()} in {days_to}d — pre-event chop, size down adds"),
                      ("cash", +1, "event_horizon",
                       f"{str(ev[2]).upper()} imminent — keep powder for the print")]
    else:
        signals.append(_signal("event_horizon", "Next macro event", None,
                               "no calendar data yet"))

    # --- crypto large-print flow (24h) --------------------------------------------
    # trades only stores LARGE prints (streamer-side filter), so this is the
    # whale aggressor-side imbalance, not retail noise.
    row = duck.fetchone(
        "SELECT sum(CASE WHEN side = 'buy' THEN notional ELSE 0 END), "
        "sum(CASE WHEN side = 'sell' THEN notional ELSE 0 END), max(ts) "
        "FROM trades WHERE ts >= ?",
        [now - timedelta(days=1)],
    )
    if row and (row[0] or row[1]):
        buys, sells = float(row[0] or 0), float(row[1] or 0)
        total = buys + sells
        imb = (buys - sells) / total if total else 0.0
        sig = _signal("crypto_flow", "Crypto whale flow (24h)", f"{imb:+.0%}",
                      f"${buys / 1e6:.1f}M bought vs ${sells / 1e6:.1f}M sold (large prints)",
                      row[2], max_age_days=1)
        signals.append(sig)
        if not sig["stale"] and total >= 2e6:
            if imb >= 0.3:
                tilts.append(("crypto", +2, "crypto_flow",
                              f"large-print flow {imb:+.0%} — whales accumulating"))
            elif imb <= -0.3:
                tilts.append(("crypto", -2, "crypto_flow",
                              f"large-print flow {imb:+.0%} — whales distributing"))
    else:
        signals.append(_signal("crypto_flow", "Crypto whale flow (24h)", None,
                               "no large prints in 24h"))

    # --- Fed statement delta --------------------------------------------------------
    row = duck.fetchone(
        "SELECT similarity, filed_at FROM filings_diff WHERE symbol = '_FOMC' "
        "AND similarity IS NOT NULL ORDER BY filed_at DESC LIMIT 1"
    )
    if row:
        fed_sim = float(row[0])
        sig = _signal("fed_statement", "Fed statement delta",
                      f"{(1 - fed_sim):.0%} changed",
                      f"latest FOMC statement vs prior ({fed_sim:.0%} similar)",
                      row[1], max_age_days=70)  # meetings are ~6-8 weeks apart
        signals.append(sig)
        if not sig["stale"] and fed_sim < 0.70:
            tilts.append(("cash", +1, "fed_statement",
                          f"Fed rewrote {(1 - fed_sim):.0%} of its statement — "
                          "policy messaging in motion, wait for repricing"))
    else:
        signals.append(_signal("fed_statement", "Fed statement delta", None,
                               "no statement diff stored yet"))

    # --- benchmark seasonality (current month) -------------------------------------
    season = compute_seasonality(duck, [benchmark])
    if season and season[0]["months"]:
        m = season[0]["months"][0]
        sig = _signal("seasonality", f"{benchmark} seasonality",
                      f"{m['win_rate']:.0f}% win",
                      f"month {m['month']}: avg {m['mean_return']:+.1f}% over {m['years']}y",
                      now, max_age_days=1)
        signals.append(sig)
        if m["win_rate"] >= 65 and m["mean_return"] > 0:
            tilts.append(("equities", +1, "seasonality",
                          f"{benchmark} closed month {m['month']} green "
                          f"{m['win_rate']:.0f}% of the last {m['years']}y"))
        elif m["win_rate"] <= 40 and m["mean_return"] < 0:
            tilts.append(("equities", -1, "seasonality",
                          f"{benchmark} historically weak in month {m['month']} "
                          f"({m['win_rate']:.0f}% win rate, {m['mean_return']:+.1f}% avg)"))
    else:
        signals.append(_signal("seasonality", f"{benchmark} seasonality", None,
                               "needs 3y+ of benchmark history"))

    # --- retail froth ----------------------------------------------------------
    spikes = duck.fetchall(
        """
        SELECT count(*) , max(ts) FROM ts_retail
        WHERE source = 'apewisdom'
          AND ts = (SELECT max(ts) FROM ts_retail WHERE source = 'apewisdom')
          AND mentions_prev >= 20 AND mentions >= 3 * mentions_prev
        """
    )
    n_spikes = int(spikes[0][0]) if spikes and spikes[0][0] is not None else 0
    sig = _signal("retail", "Retail spikes (3x+)", n_spikes,
                  "tickers with mentions 3x+ vs 24h ago",
                  spikes[0][1] if spikes else None, max_age_days=2)
    signals.append(sig)
    if not sig["stale"] and n_spikes >= 3:
        tilts.append(("equities", -1, "retail",
                      f"{n_spikes} retail mention spikes — froth, late-crowd risk"))

    # --- insider clusters (informed buying) -------------------------------------
    rows = duck.fetchall(
        "SELECT count(DISTINCT symbol), max(filed_at) FROM insider_trades "
        "WHERE filed_at >= ?",
        [now - timedelta(days=14)],
    )
    n_insider = int(rows[0][0]) if rows and rows[0][0] is not None else 0
    sig = _signal("insider", "Insider buy clusters (14d)", n_insider,
                  "issuers with open-market Form 4 buys",
                  rows[0][1] if rows else None, max_age_days=15)
    signals.append(sig)
    if not sig["stale"] and n_insider >= 2:
        tilts.append(("equities", +2, "insider",
                      f"insiders buying {n_insider} issuers — informed conviction"))

    # --- whales + congress (context, small/no tilt) ------------------------------
    whale_new = whale_new_positions(duck, days=30, top_rank=10)
    signals.append(_signal(
        "whales", "Whale new top-10 positions (30d)", len(whale_new),
        "; ".join(f"{w['fund']}→{w['issuer']}" for w in whale_new[:4]) or "none",
        max(w["filed_at"] for w in whale_new) if whale_new else None,
        max_age_days=31,
    ))
    if whale_new and len(whale_new) >= 3:
        tilts.append(("equities", +1, "whales",
                      f"{len(whale_new)} new whale top-10 positions — institutional conviction"))

    watch = {r[0].upper() for r in sqlite.fetchall("SELECT symbol FROM watchlist")}
    hits = duck.fetchall(
        "SELECT ticker, max(filed_at) FROM congress_trades "
        "WHERE ticker IS NOT NULL AND filed_at >= ? GROUP BY ticker",
        [now - timedelta(days=7)],
    )
    hits = [h for h in hits if str(h[0]).upper() in watch]
    signals.append(_signal(
        "congress", "Congress watchlist hits (7d)", len(hits),
        ", ".join(str(h[0]) for h in hits[:6]) or "none",
        max((h[1] for h in hits), default=None), max_age_days=8,
    ))

    # --- apply tilts -----------------------------------------------------------
    base = BASE_ALLOCATION.get(regime, BASE_ALLOCATION["unknown"])
    weights = dict(base)
    reasons: dict[str, list[dict]] = {k: [] for k, _ in BUCKETS}
    for key, _label in BUCKETS:
        reasons[key].append({
            "signal": "composite",
            "detail": f"base allocation for regime '{regime}'"
                      + (f" (composite {score:+})" if score is not None else ""),
            "delta": 0.0,
        })
    for bucket, delta, sig_key, reason in tilts:
        weights[bucket] = weights.get(bucket, 0) + delta
        reasons[bucket].append({"signal": sig_key, "detail": reason, "delta": delta})

    # Clamp >= 0 and renormalize to 100.
    for k in weights:
        weights[k] = max(0.0, weights[k])
    total = sum(weights.values()) or 1.0
    weights = {k: v / total * 100.0 for k, v in weights.items()}

    # --- POSTURE gross scaling (de-risk the equity sleeve to match the tide) ---
    # Scale the EQUITY bucket weight by posture['gross_factor'] (defensive -> 0.3,
    # neutral -> 0.65, aggressive -> 1.0). Because each equity holding's weight_pct
    # is the bucket weight x its sleeve share, scaling the bucket here scales every
    # equity holding proportionally. The freed gross moves into the cash/short-
    # duration sleeve (the existing 'cash' bucket = SGOV) so the book stays fully
    # allocated. Only EQUITIES are scaled — gold/crypto/cash are left untouched.
    if posture is not None:
        try:
            gf = float(posture.get("gross_factor", 1.0))
            if gf < 1.0 and "equities" in weights:
                freed = weights["equities"] * (1.0 - gf)
                weights["equities"] *= gf
                weights["cash"] = weights.get("cash", 0.0) + freed  # shift to safe sleeve
        except Exception as exc:  # never let posture break the allocation
            log.warning("posture gross scaling failed (%s)", exc)

    # --- fundamental factor screen (descriptive, feeds picks) -----------------
    factor_leaders: list[dict] = []
    try:
        from app.ml.factors import latest_ranks as _factor_ranks
        fr = _factor_ranks(top=8)
        factor_leaders = fr.get("leaders", [])
        signals.append(_signal(
            "factors", "Factor screen leaders", len(factor_leaders),
            ", ".join(p["symbol"] for p in factor_leaders[:6]) or "none",
            fr.get("as_of"), max_age_days=45))
    except Exception as exc:  # missing fundamentals DB — fail soft
        log.warning("factor screen failed (%s)", exc)
        signals.append(_signal("factors", "Factor screen leaders", None,
                               "fundamentals not loaded"))

    # --- individual holdings inside each sleeve --------------------------------
    picks = _equity_stock_picks(duck, sqlite, benchmark, now, factor_leaders=factor_leaders)
    signals.append(_signal(
        "picks", "Single-name conviction", len(picks),
        ", ".join(f"{p['symbol']} ({p['score']:.1f})" for p in picks)
        or "no name clears the multi-signal bar",
        now, max_age_days=1,
    ))
    # Single names cap at ~10% of the equity sleeve each (40% combined,
    # score-weighted); the remainder rides in RRG-weighted sector ETFs.
    stock_share = min(40.0, 10.0 * len(picks))
    holdings_eq: list[dict] = []
    if picks:
        score_total = sum(p["score"] for p in picks) or 1.0
        holdings_eq = [
            {
                "symbol": p["symbol"], "name": None, "kind": "stock",
                "share": stock_share * p["score"] / score_total,
                "score": round(p["score"], 1), "evidence": p["evidence"],
            }
            for p in picks
        ]
    holdings_eq += [
        {**s, "share": s["share"] * (100.0 - stock_share) / 100.0}
        for s in _sector_sleeve(rrg)
    ]

    # --- POSTURE lifecycle tags (enter/hold/trim/exit per equity name) --------
    # Map each equity holding to an RRG quadrant: a sector ETF IS a sector, so it
    # uses its own quadrant; a single name has no grid read (quadrant=None) and
    # the classifier falls back to the weekly trend. Combined with the monthly +
    # weekly posture states this tells the swing bot what to do with each name.
    if posture is not None:
        try:
            quad_by_sym = {s["symbol"]: s.get("quadrant")
                           for s in rrg.get("sectors", [])}
            m_state = posture.get("monthly", {}).get("state", "unknown")
            w_state = posture.get("weekly", {}).get("state", "unknown")
            p_state = posture.get("state", "neutral")
            for h in holdings_eq:
                quad = quad_by_sym.get(h["symbol"]) if h.get("kind") == "sector" else None
                h["lifecycle"] = name_lifecycle(quad, m_state, w_state, p_state)
        except Exception as exc:  # tags are best-effort, never fatal
            log.warning("posture lifecycle tagging failed (%s)", exc)

    holdings_by_bucket = {
        "equities": holdings_eq,
        "metals": _metals_split(duck, extremes),
        "crypto": _crypto_split(duck, extremes),
        "cash": _cash_sleeve(),
    }

    buckets_out = [
        {
            "key": key,
            "label": label,
            "base_pct": float(base.get(key, 0)),
            "weight_pct": round(weights.get(key, 0.0), 1),
            "reasons": reasons[key],
            "holdings": [
                {
                    "symbol": h["symbol"],
                    "name": h.get("name"),
                    "kind": h["kind"],
                    "sleeve_pct": round(h["share"], 1),
                    "weight_pct": round(weights.get(key, 0.0) * h["share"] / 100.0, 1),
                    "score": h.get("score"),
                    "evidence": h["evidence"],
                    # POSTURE lifecycle (enter/hold/trim/exit + trend_strength);
                    # only equity holdings carry it, others are None.
                    "lifecycle": h.get("lifecycle"),
                }
                for h in holdings_by_bucket.get(key, [])
            ],
        }
        for key, label in BUCKETS
    ]

    return {
        "as_of": now.isoformat(timespec="minutes"),
        "regime": regime,
        "score": score,
        "buckets": buckets_out,
        "equity_tilt": equity_tilt,
        # The shared macro POSTURE the whole book runs on (None if the engine
        # failed). Added field — existing snapshot consumers are unaffected.
        "posture": posture,
        "signals": signals,
        "disclaimer": DISCLAIMER,
    }


def build_template_notes(result: dict) -> list[str]:
    """Deterministic 3-5 strategy notes — the LLM fallback, never empty."""
    notes: list[str] = []
    sig = {s["key"]: s for s in result["signals"]}

    def fresh(key: str) -> dict | None:
        s = sig.get(key)
        return s if s and not s["stale"] and s["value"] is not None else None

    regime, score = result["regime"], result["score"]
    notes.append(
        f"Regime {regime.upper()}"
        + (f" (composite {score:+})" if score is not None else "")
        + f" — base mix {', '.join(f'{b['label']} {b['weight_pct']:.0f}%' for b in result['buckets'])}."
    )
    g = fresh("gex")
    if g and g["value"] == "short":
        notes.append("Dealers short gamma + " + regime +
                     ": size down, expect range expansion until spot reclaims the flip.")
    nl = fresh("net_liquidity")
    if nl and isinstance(nl["value"], (int, float)):
        if nl["value"] <= -150:
            notes.append(f"Net liquidity {nl['value']:+,} $bn/1m — drain regime; "
                         "fade rallies in high-beta until the 1m delta turns.")
        elif nl["value"] >= 50:
            notes.append(f"Net liquidity {nl['value']:+,} $bn/1m — tailwind; "
                         "dips in BTC/quality risk are buyable with ~5wk lead.")
    sr = fresh("stress_radar")
    if sr and sr["value"] == "ON":
        notes.append("Stress radar lit (yen + vol term + gold/silver) — treat "
                     "correlation breaks as structural, do not fade.")
    cb = fresh("cookbook")
    if cb and isinstance(cb["value"], int) and cb["value"] >= 3:
        notes.append(f"{cb['value']} cookbook cards BROKEN — cross-asset hedges "
                     "may not behave; reduce gross rather than re-hedging.")
    eq = next((b for b in result["buckets"] if b["key"] == "equities"), None)
    stocks = [h for h in (eq or {}).get("holdings", []) if h["kind"] == "stock"]
    if stocks:
        notes.append(
            "Single names: "
            + ", ".join(f"{h['symbol']} {h['weight_pct']:.0f}%" for h in stocks[:3])
            + f" — {stocks[0]['symbol']}: {stocks[0]['evidence'][0]}."
        )
    tilt = result.get("equity_tilt")
    if tilt and tilt.get("favor"):
        notes.append("Equity sleeve: favor " + ", ".join(tilt["favor"][:3])
                     + (("; avoid " + ", ".join(tilt["avoid"][:2])) if tilt.get("avoid") else "")
                     + " (RRG).")
    return notes[:5]


class StrategistService:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        hub: ConnectionManager,
        llm: LlmClient,
        *,
        sectors: list[str],
        benchmark: str,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._hub = hub
        self._llm = llm
        self._sectors = sectors
        self._benchmark = benchmark

    async def _notes(self, result: dict) -> tuple[list[str], str]:
        try:
            text = await self._llm.generate(
                _NOTES_PROMPT.format(data=json.dumps(
                    {k: result[k] for k in ("regime", "score", "buckets", "equity_tilt", "signals")},
                    indent=1)),
            )
            notes = [ln.lstrip("-• ").strip() for ln in text.splitlines()
                     if ln.strip().startswith(("-", "•"))]
            if not 3 <= len(notes) <= 8:
                raise ValueError(f"LLM returned {len(notes)} notes")
            return notes[:5], self._llm.label
        except Exception as exc:
            log.warning("%s strategist notes failed (%s) — using template",
                        self._llm.label, exc)
            return build_template_notes(result), "template"

    async def run(self) -> dict:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, compute_strategist,
            self._duck, self._sqlite, self._sectors, self._benchmark,
        )
        notes, model = await self._notes(result)
        result["notes"] = notes
        result["model"] = model

        day = datetime(*date.today().timetuple()[:3])
        await loop.run_in_executor(
            None, self._duck.execute,
            "INSERT OR REPLACE INTO strategist_snapshots (ts, regime, model, detail) "
            "VALUES (?, ?, ?, ?)",
            [day, result["regime"], model, json.dumps(result)],
        )
        await self._hub.broadcast(STRATEGIST_TOPIC, {
            "type": "strategist", "date": day.strftime("%Y-%m-%d"),
            "regime": result["regime"], "model": model,
        })
        log.info("strategist snapshot for %s (regime %s, model %s)",
                 day.date(), result["regime"], model)
        return result


def latest_snapshot(duck: DuckStore) -> dict | None:
    row = duck.fetchone(
        "SELECT detail FROM strategist_snapshots ORDER BY ts DESC LIMIT 1"
    )
    return json.loads(row[0]) if row and row[0] else None
