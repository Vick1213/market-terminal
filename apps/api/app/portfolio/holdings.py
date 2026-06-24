"""Portfolio P&L + exposure + strategist-drift compute (blocking, no network).

Every figure is derived from data the other pipelines already store:
  * positions          — SQLite (the user's manual/CSV entries)
  * latest close + prev — DuckDB ts_price (the 'yahoo' daily-bar namespace)
  * regime              — DuckDB macro_composite
  * target allocation   — the latest strategist snapshot

``compute_portfolio`` takes a pre-built ``price_map`` so the caller decides
the price source: the REST router overlays live quotes (network), while the
alert engine passes cached closes only (it must never touch the network).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore

# asset_class -> strategist allocation bucket. fx is cash-like (currency
# exposure parked, no duration); futures default to the equity sleeve.
BUCKET_OF_CLASS = {
    "equity": "equities",
    "metal": "metals",
    "crypto": "crypto",
    "fx": "cash",
    "future": "equities",
}

# Bucket order + labels mirror edge/strategist.BUCKETS so the drift table lines
# up 1:1 with the Strategist panel (kept local to avoid an import cycle).
_BUCKETS = [
    ("equities", "Equities"),
    ("metals", "Gold / Silver"),
    ("crypto", "BTC / ETH"),
    ("cash", "Cash / short duration"),
]

# Cash-like holdings map to the cash bucket regardless of their asset_class so
# a T-bill ETF position offsets the strategist's cash target (the strategist
# itself parks cash in SGOV — see edge/strategist._cash_sleeve).
_CASH_LIKE = {"SGOV", "BIL", "SHV", "SHY", "USFR", "TFLO", "CASH", "VMFXX", "SPAXX"}

DISCLAIMER = (
    "P&L and exposure are computed from cached daily closes (equities delayed); "
    "drift compares your weights to the strategist's suggested allocation, which "
    "is signal synthesis, NOT financial advice. Cost basis is whatever you entered."
)


def _bucket_of(symbol: str, asset_class: str) -> str:
    if symbol.upper() in _CASH_LIKE:
        return "cash"
    return BUCKET_OF_CLASS.get(asset_class, "equities")


def latest_closes(duck: DuckStore, symbols: list[str]) -> dict[str, dict]:
    """{symbol: {"price", "prev_close"}} from ts_price (blocking, no network).

    Tries the symbol as stored and its yahoo dash form (crypto is kept as
    BTC/USD on the watchlist but daily bars may land under either)."""
    out: dict[str, dict] = {}
    for symbol in dict.fromkeys(symbols):
        for sym in dict.fromkeys([symbol, symbol.replace("/", "-")]):
            rows = duck.fetchall(
                "SELECT close FROM ts_price WHERE source = 'yahoo' AND symbol = ? "
                "AND close IS NOT NULL ORDER BY ts DESC LIMIT 2",
                [sym],
            )
            if rows:
                out[symbol] = {
                    "price": float(rows[0][0]),
                    "prev_close": float(rows[1][0]) if len(rows) > 1 else None,
                }
                break
    return out


def _regime(duck: DuckStore) -> str:
    row = duck.fetchone(
        "SELECT regime FROM macro_composite ORDER BY ts DESC LIMIT 1"
    )
    return row[0] if row and row[0] else "unknown"


def _strategist_targets(duck: DuckStore) -> tuple[dict[str, float], str | None]:
    """{bucket_key: target weight_pct} + the snapshot's as_of, from the latest
    strategist snapshot (empty until the strategist has run once)."""
    import json

    row = duck.fetchone(
        "SELECT ts, detail FROM strategist_snapshots ORDER BY ts DESC LIMIT 1"
    )
    if not row or not row[1]:
        return {}, None
    try:
        snap = json.loads(row[1])
    except ValueError:
        return {}, None
    targets = {
        b["key"]: float(b["weight_pct"])
        for b in snap.get("buckets") or []
        if b.get("key") is not None and b.get("weight_pct") is not None
    }
    return targets, str(row[0])[:10]


def _positions(sqlite: SqliteStore) -> list[dict]:
    return [
        {
            "id": int(r["id"]),
            "symbol": r["symbol"],
            "asset_class": r["asset_class"],
            "quantity": float(r["quantity"]),
            "cost_basis": float(r["cost_basis"]) if r["cost_basis"] is not None else None,
            "display_name": r["display_name"],
            "opened_at": r["opened_at"],
            "note": r["note"],
        }
        for r in sqlite.fetchall(
            "SELECT id, symbol, asset_class, quantity, cost_basis, display_name, "
            "opened_at, note FROM positions ORDER BY symbol, id"
        )
    ]


def compute_portfolio(
    duck: DuckStore, sqlite: SqliteStore, price_map: dict[str, dict]
) -> dict:
    """The full portfolio snapshot. Blocking — dispatch via run_in_executor."""
    now = datetime.now(timezone.utc)
    rows = _positions(sqlite)
    targets, strat_as_of = _strategist_targets(duck)
    regime = _regime(duck)

    positions: list[dict] = []
    total_value = 0.0
    total_cost = 0.0      # only over positions that have BOTH price and cost
    total_pnl = 0.0
    day_pnl = 0.0
    prev_value = 0.0      # value at yesterday's close (for day %)
    priced = 0
    any_live = False

    # bucket_key -> market value, for exposure + drift.
    bucket_value: dict[str, float] = {k: 0.0 for k, _ in _BUCKETS}
    class_value: dict[str, float] = {}

    for p in rows:
        q = p["quantity"]
        cost = p["cost_basis"]
        pm = price_map.get(p["symbol"]) or price_map.get(p["symbol"].replace("/", "-"))
        price = pm.get("price") if pm else None
        prev_close = pm.get("prev_close") if pm else None
        live = bool(pm.get("live")) if pm else False
        any_live = any_live or live

        mv = price * q if price is not None else None
        cv = cost * q if cost is not None else None
        pnl = mv - cv if (mv is not None and cv is not None) else None
        pnl_pct = (pnl / cv * 100.0) if (pnl is not None and cv) else None
        day_chg_pct = (
            (price - prev_close) / prev_close * 100.0
            if (price is not None and prev_close)
            else None
        )
        pos_day_pnl = (price - prev_close) * q if (price is not None and prev_close is not None) else None

        if mv is not None:
            priced += 1
            total_value += mv
            bucket = _bucket_of(p["symbol"], p["asset_class"])
            bucket_value[bucket] = bucket_value.get(bucket, 0.0) + mv
            class_value[p["asset_class"]] = class_value.get(p["asset_class"], 0.0) + mv
        if pnl is not None:
            total_cost += cv
            total_pnl += pnl
        if pos_day_pnl is not None:
            day_pnl += pos_day_pnl
            prev_value += prev_close * q

        positions.append({
            **p,
            "price": price,
            "prev_close": prev_close,
            "live": live,
            "market_value": mv,
            "cost_value": cv,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "day_change_pct": day_chg_pct,
            "day_pnl": pos_day_pnl,
            "weight_pct": None,  # filled below once total_value is known
        })

    for p in positions:
        if p["market_value"] is not None and total_value:
            p["weight_pct"] = p["market_value"] / total_value * 100.0

    exposure = [
        {
            "asset_class": cls,
            "value": val,
            "pct": (val / total_value * 100.0) if total_value else 0.0,
        }
        for cls, val in sorted(class_value.items(), key=lambda kv: kv[1], reverse=True)
    ]

    drift = []
    for key, label in _BUCKETS:
        actual = (bucket_value.get(key, 0.0) / total_value * 100.0) if total_value else 0.0
        target = targets.get(key)
        drift.append({
            "key": key,
            "label": label,
            "actual_pct": round(actual, 1),
            "target_pct": round(target, 1) if target is not None else None,
            "drift_pp": round(actual - target, 1) if target is not None else None,
        })

    return {
        "as_of": now.isoformat(timespec="seconds"),
        "regime": regime,
        "strategist_as_of": strat_as_of,
        "positions": positions,
        "summary": {
            "n_positions": len(rows),
            "priced": priced,
            "any_live": any_live,
            "total_value": round(total_value, 2),
            "total_cost": round(total_cost, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / total_cost * 100.0, 2) if total_cost else None,
            "day_pnl": round(day_pnl, 2),
            "day_pnl_pct": round(day_pnl / prev_value * 100.0, 2) if prev_value else None,
        },
        "exposure": exposure,
        "drift": drift,
        "disclaimer": DISCLAIMER,
    }
