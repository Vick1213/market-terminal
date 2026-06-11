"""Alert engine (PLAN §6 #3 + #7 + regime gating).

A scheduled sweep evaluates rules over data the other pipelines already
store — nothing here makes a network request. Every rule instance has a
``rule_key``; ``alert_state`` (SQLite) remembers the last fired time and the
last observed value so threshold rules fire on the *crossing*, not on every
sweep above it, and everything respects a cooldown.

Fired alerts are persisted to DuckDB (the panel feed), broadcast on WS topic
``alerts``, and — for warn/critical — pushed to ntfy when a topic is set.

Regime gating: divergence/reversion suggestions are suppressed or reworded
when the macro regime says stress (don't fade a structural break, PLAN §3f).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.corr.cards import CARDS_BY_ID
from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.edge.ntfy import NtfyPublisher
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.edge.alerts")

ALERTS_TOPIC = "alerts"

# ts_macro series swept for level z-spikes: id -> (label, warn_z, critical_z).
_Z_SERIES = {
    "VIX": ("VIX", 2.5, 4.0),
    "BAMLH0A0HYM2": ("HY credit spread", 2.0, 3.0),
    "PC_TOTAL": ("Total put/call", 2.5, 4.0),
    "NFCI": ("Financial conditions (NFCI)", 2.0, 3.0),
    "DTWEXBGS": ("Broad dollar", 2.5, 4.0),
}


@dataclass
class Alert:
    rule: str
    key: str               # rule-instance key for dedupe/cooldown
    severity: str          # info | warn | critical
    title: str
    body: str
    symbol: str | None = None
    value: float | None = None
    detail: dict = field(default_factory=dict)


class AlertEngine:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        hub: ConnectionManager,
        ntfy: NtfyPublisher,
        *,
        cooldown_hours: int = 24,
        large_print_notional: float = 250_000,
        insider_cluster_min_buyers: int = 2,
        insider_cluster_window_days: int = 14,
        netliq_drain_bn: float = -150.0,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._hub = hub
        self._ntfy = ntfy
        self._cooldown = timedelta(hours=cooldown_hours)
        self._large_notional = large_print_notional
        self._cluster_min = insider_cluster_min_buyers
        self._cluster_window = insider_cluster_window_days
        self._netliq_drain = netliq_drain_bn

    # ------------------------------------------------------------------ state

    def _state(self, key: str) -> tuple[datetime | None, float | None, str | None]:
        row = self._sqlite.fetchone(
            "SELECT last_fired, last_value, last_text FROM alert_state WHERE rule_key = ?",
            [key],
        )
        if row is None:
            return None, None, None
        fired = datetime.fromisoformat(row[0]) if row[0] else None
        return fired, row[1], row[2]

    def _save_state(self, key: str, *, fired: datetime | None = None,
                    value: float | None = None, text: str | None = None) -> None:
        prev_fired, prev_value, prev_text = self._state(key)
        self._sqlite.execute(
            "INSERT OR REPLACE INTO alert_state (rule_key, last_fired, last_value, last_text) "
            "VALUES (?, ?, ?, ?)",
            [
                key,
                (fired or prev_fired).isoformat() if (fired or prev_fired) else None,
                value if value is not None else prev_value,
                text if text is not None else prev_text,
            ],
        )

    def _cooled(self, key: str, now: datetime) -> bool:
        fired, _, _ = self._state(key)
        return fired is None or now - fired >= self._cooldown

    def _fired_ever(self, key: str) -> bool:
        """One-shot events (a specific PTR row, a specific 13F position) fire
        once per key forever — a cooldown would re-fire them daily."""
        return self._state(key)[0] is not None

    # ------------------------------------------------------------------ rules

    def _regime(self) -> str:
        row = self._duck.fetchone(
            "SELECT regime FROM macro_composite ORDER BY ts DESC LIMIT 1"
        )
        return row[0] if row and row[0] else "unknown"

    def _rule_regime_flip(self, now: datetime, regime: str) -> list[Alert]:
        _, _, prev = self._state("regime_flip")
        self._save_state("regime_flip", text=regime)
        if prev is None or prev == regime or regime == "unknown":
            return []
        sev = "critical" if regime == "stress" else "warn"
        return [Alert(
            rule="regime_flip", key="regime_flip", severity=sev,
            title=f"Regime flip: {prev} → {regime}",
            body=f"Macro composite regime changed from {prev} to {regime}. "
                 "Cookbook reversion plays and risk sizing should be re-checked.",
        )]

    def _rule_macro_z(self, now: datetime, regime: str) -> list[Alert]:
        out: list[Alert] = []
        for sid, (label, warn_z, crit_z) in _Z_SERIES.items():
            df = self._duck.fetchdf(
                "SELECT ts, value FROM ts_macro WHERE series_id = ? "
                "AND ts >= ? ORDER BY ts",
                [sid, now - timedelta(days=180)],
            )
            if df is None or len(df) < 60:
                continue
            s = df["value"]
            mu, sd = s.iloc[:-1].mean(), s.iloc[:-1].std()
            if not sd or sd != sd:
                continue
            z = float((s.iloc[-1] - mu) / sd)
            key = f"macro_z:{sid}"
            _, prev_z, _ = self._state(key)
            self._save_state(key, value=z)
            if abs(z) < warn_z or (prev_z is not None and abs(prev_z) >= warn_z):
                continue  # not extreme, or already in the extreme state
            if not self._cooled(key, now):
                continue
            sev = "critical" if abs(z) >= crit_z else "warn"
            out.append(Alert(
                rule="macro_z", key=key, severity=sev,
                title=f"{label} z-spike: {z:+.1f}σ",
                body=f"{label} ({sid}) is {z:+.1f}σ vs its 6-month range "
                     f"(last {float(s.iloc[-1]):g}). Regime: {regime}.",
                value=round(z, 2),
            ))
        return out

    def _rule_vix_term(self, now: datetime, regime: str) -> list[Alert]:
        """VIX3M/VIX < 1.0 = backwardation, the early risk-off tripwire."""
        ratio = None
        legs = {}
        for sid in ("VIX", "VIX3M"):
            row = self._duck.fetchone(
                "SELECT value FROM ts_macro WHERE series_id = ? ORDER BY ts DESC LIMIT 1",
                [sid],
            )
            if row is None or row[0] is None:
                return []
            legs[sid] = float(row[0])
        ratio = legs["VIX3M"] / legs["VIX"] if legs["VIX"] else None
        if ratio is None:
            return []
        key = "vix_term"
        _, prev, _ = self._state(key)
        self._save_state(key, value=ratio)
        if prev is None:
            return []
        out: list[Alert] = []
        if prev >= 1.0 > ratio and self._cooled(key, now):
            out.append(Alert(
                rule="vix_term", key=key, severity="critical",
                title=f"VIX term structure inverted ({ratio:.2f})",
                body=f"VIX3M/VIX crossed below 1.0 ({ratio:.2f}) — backwardation, "
                     "the cookbook's early risk-off tripwire (card 11).",
                value=round(ratio, 3),
            ))
        elif prev < 1.0 <= ratio and self._cooled(key, now):
            out.append(Alert(
                rule="vix_term", key=key, severity="info",
                title=f"VIX term structure normalized ({ratio:.2f})",
                body="VIX3M/VIX back above 1.0 — contango restored.",
                value=round(ratio, 3),
            ))
        return out

    def _rule_corr_break(self, now: datetime, regime: str) -> list[Alert]:
        """Card status transitions into BROKEN, regime-gated wording."""
        df = self._duck.fetchdf(
            """
            SELECT card_id, ts, status, z FROM corr_snapshots
            WHERE card_id != '_meta' AND ts >= ?
            ORDER BY card_id, ts
            """,
            [now - timedelta(days=7)],
        )
        if df is None or df.empty:
            return []
        out: list[Alert] = []
        for card_id, g in df.groupby("card_id"):
            if len(g) < 2:
                continue
            curr, prev = g.iloc[-1], g.iloc[-2]
            if curr["status"] != "broken" or prev["status"] == "broken":
                continue
            key = f"corr_break:{card_id}"
            if not self._cooled(key, now):
                continue
            spec = CARDS_BY_ID.get(card_id)
            title = spec.title if spec else card_id
            z = float(curr["z"]) if curr["z"] == curr["z"] else None
            # The §3f gate: in stress, a break is structural — do not fade it.
            if regime == "stress":
                hint = "Regime is STRESS — treat as structural, do not fade."
                sev = "critical"
            else:
                hint = f"Regime is {regime} — divergence may be a reversion candidate."
                sev = "warn"
            out.append(Alert(
                rule="corr_break", key=key, severity=sev,
                title=f"Correlation BROKEN: {title}",
                body=(f"Cookbook card '{title}' flipped to BROKEN"
                      + (f" (z {z:+.1f})" if z is not None else "") + f". {hint}"),
                value=z,
            ))
        return out

    def _rule_retail_spike(self, now: datetime, regime: str) -> list[Alert]:
        """Mention volume >=3x its 24h-ago print on real volume."""
        df = self._duck.fetchdf(
            """
            SELECT symbol, mentions, mentions_prev FROM ts_retail
            WHERE source = 'apewisdom'
              AND ts = (SELECT max(ts) FROM ts_retail WHERE source = 'apewisdom')
              AND mentions >= 150 AND mentions_prev >= 20
            """
        )
        if df is None or df.empty:
            return []
        out: list[Alert] = []
        for _, r in df.iterrows():
            ratio = r["mentions"] / r["mentions_prev"]
            if ratio < 3.0:
                continue
            key = f"retail_spike:{r['symbol']}"
            if not self._cooled(key, now):
                continue
            out.append(Alert(
                rule="retail_spike", key=key, severity="warn",
                title=f"Retail spike: {r['symbol']} {ratio:.1f}x",
                body=f"{r['symbol']} mentions {int(r['mentions'])} vs "
                     f"{int(r['mentions_prev'])} a day ago ({ratio:.1f}x). "
                     "Check the Retail panel drill-down for text sentiment.",
                symbol=str(r["symbol"]), value=round(float(ratio), 1),
            ))
        return out

    def _rule_insider_cluster(self, now: datetime, regime: str) -> list[Alert]:
        df = self._duck.fetchdf(
            """
            SELECT symbol,
                   count(DISTINCT insider_name)        AS buyers,
                   sum(value)                          AS total_value,
                   max(CASE WHEN is_ceo_cfo THEN 1 ELSE 0 END) AS has_ceo_cfo
            FROM insider_trades
            WHERE filed_at >= ?
            GROUP BY symbol
            """,
            [now - timedelta(days=self._cluster_window)],
        )
        if df is None or df.empty:
            return []
        out: list[Alert] = []
        for _, r in df.iterrows():
            cluster = int(r["buyers"]) >= self._cluster_min
            ceo_cfo = bool(r["has_ceo_cfo"])
            if not cluster and not ceo_cfo:
                continue
            key = f"insider:{r['symbol']}:{int(r['buyers'])}"
            if not self._cooled(key, now):
                continue
            kind = ("cluster buy" if cluster else "CEO/CFO buy")
            total = float(r["total_value"] or 0)
            out.append(Alert(
                rule="insider_cluster", key=key, severity="warn",
                title=f"Insider {kind}: {r['symbol']}",
                body=f"{int(r['buyers'])} insider(s) bought ~${total:,.0f} of "
                     f"{r['symbol']} on the open market (Form 4 code P) in the last "
                     f"{self._cluster_window}d.",
                symbol=str(r["symbol"]), value=total,
            ))
        return out

    def _rule_cot_extreme(self, now: datetime, regime: str) -> list[Alert]:
        df = self._duck.fetchdf(
            "SELECT market_code, market, ts, noncomm_long - noncomm_short AS net "
            "FROM ts_cot WHERE ts >= ? ORDER BY market_code, ts",
            [now - timedelta(days=3 * 365)],
        )
        if df is None or df.empty:
            return []
        out: list[Alert] = []
        for code, g in df.groupby("market_code"):
            if len(g) < 52:
                continue
            net = g["net"]
            lo, hi = net.min(), net.max()
            if hi == lo:
                continue
            idx = float(100 * (net.iloc[-1] - lo) / (hi - lo))
            if 10 < idx < 90:
                continue
            key = f"cot:{code}"
            _, prev, _ = self._state(key)
            self._save_state(key, value=idx)
            if prev is not None and (prev <= 10 or prev >= 90):
                continue  # already extreme last sweep
            if not self._cooled(key, now):
                continue
            market = g["market"].iloc[-1]
            side = "max bullish" if idx >= 90 else "max bearish"
            out.append(Alert(
                rule="cot_extreme", key=key, severity="warn",
                title=f"COT extreme: {market} index {idx:.0f}",
                body=f"Non-commercial positioning in {market} is at its 3-year "
                     f"{side} extreme (COT index {idx:.0f}). Crowded trades "
                     "mean-revert — watch for a positioning unwind.",
                symbol=str(market), value=round(idx, 1),
            ))
        return out

    def _rule_gex_flip(self, now: datetime, regime: str) -> list[Alert]:
        row = self._duck.fetchone(
            "SELECT spot, flip, total_gex FROM gex_snapshots "
            "WHERE symbol = '_SPX' ORDER BY ts DESC LIMIT 1"
        )
        if row is None or row[0] is None or row[1] is None:
            return []
        spot, flip = float(row[0]), float(row[1])
        below = 1.0 if spot < flip else 0.0
        key = "gex_flip"
        _, prev, _ = self._state(key)
        self._save_state(key, value=below)
        if prev is None or prev == below or not self._cooled(key, now):
            return []
        if below:
            return [Alert(
                rule="gex_flip", key=key, severity="warn",
                title=f"SPX below gamma flip ({flip:,.0f})",
                body=f"SPX spot {spot:,.0f} crossed below the gamma-flip strike "
                     f"{flip:,.0f} — dealers short gamma, expect amplified moves "
                     "and wider ranges.",
                value=flip,
            )]
        return [Alert(
            rule="gex_flip", key=key, severity="info",
            title=f"SPX back above gamma flip ({flip:,.0f})",
            body=f"SPX spot {spot:,.0f} reclaimed the gamma-flip strike — dealers "
                 "long gamma again, moves should compress.",
            value=flip,
        )]

    def _rule_large_print(self, now: datetime, regime: str) -> list[Alert]:
        """Exceptional tape: >=4x the large-print threshold since last sweep."""
        threshold = 4 * self._large_notional
        df = self._duck.fetchdf(
            "SELECT symbol, ts, price, size, side, notional FROM trades "
            "WHERE ts >= ? AND notional >= ? ORDER BY notional DESC LIMIT 3",
            [now - timedelta(minutes=15), threshold],
        )
        if df is None or df.empty:
            return []
        out: list[Alert] = []
        for _, r in df.iterrows():
            key = f"large_print:{r['symbol']}"
            if not self._cooled(key, now):
                continue
            out.append(Alert(
                rule="large_print", key=key, severity="info",
                title=f"Whale print: {r['symbol']} ${r['notional'] / 1e6:.1f}M",
                body=f"{str(r['side'] or '?').upper()} {float(r['size']):g} "
                     f"{r['symbol']} @ {float(r['price']):,.0f} "
                     f"(${float(r['notional']) / 1e6:.1f}M notional).",
                symbol=str(r["symbol"]), value=float(r["notional"]),
            ))
        return out

    # --------------------------------------------------- Phase 8/9 rules

    def _rule_netliq_drain(self, now: datetime, regime: str) -> list[Alert]:
        """NET_LIQUIDITY 20-print (~1 month) change below the threshold —
        crossing semantics: fires entering the drained state, not every sweep."""
        rows = self._duck.fetchall(
            "SELECT value FROM ts_macro WHERE series_id = 'NET_LIQUIDITY' "
            "ORDER BY ts DESC LIMIT 21"
        )
        if len(rows) < 21:
            return []
        delta = float(rows[0][0]) - float(rows[20][0])
        key = "netliq_drain"
        _, prev, _ = self._state(key)
        self._save_state(key, value=delta)
        if delta >= self._netliq_drain:
            return []
        if prev is not None and prev < self._netliq_drain:
            return []  # already in the drained state last sweep
        if not self._cooled(key, now):
            return []
        return [Alert(
            rule="netliq_drain", key=key, severity="warn",
            title=f"Net liquidity draining: {delta:+,.0f} $bn/1m",
            body=f"Daily Fed net liquidity (WALCL − TGA − RRP) is down "
                 f"{delta:+,.0f} $bn over the last 20 prints (~1 month), past the "
                 f"{self._netliq_drain:+,.0f} $bn threshold. Liquidity leads risk "
                 f"assets by ~5-6 weeks — a sustained drain is a headwind. "
                 f"Level: {float(rows[0][0]):,.0f} $bn. Regime: {regime}.",
            value=round(delta, 1),
        )]

    def _rule_congress_watchlist(self, now: datetime, regime: str) -> list[Alert]:
        """New Senate PTR row on a watchlist ticker — one alert per
        (senator, ticker, ptr_id), ever."""
        watch = {
            r[0].upper() for r in self._sqlite.fetchall("SELECT symbol FROM watchlist")
        }
        if not watch:
            return []
        rows = self._duck.fetchall(
            "SELECT ptr_id, senator, ticker, side, amount_min, amount_max, filed_at "
            "FROM congress_trades WHERE ticker IS NOT NULL AND filed_at >= ?",
            [now - timedelta(days=14)],
        )
        out: list[Alert] = []
        for ptr_id, senator, ticker, side, lo, hi, filed in rows:
            if str(ticker).upper() not in watch:
                continue
            key = f"congress_watch:{ptr_id}:{senator}:{ticker}"
            if self._fired_ever(key):
                continue
            band = ""
            if lo is not None:
                band = f" (${lo:,.0f}–${hi:,.0f})" if hi is not None else f" (>${lo:,.0f})"
            out.append(Alert(
                rule="congress_watchlist", key=key, severity="info",
                title=f"Senator traded {ticker}: {senator} {side or '?'}",
                body=f"{senator} filed a PTR ({str(filed)[:10]}) reporting a "
                     f"{side or 'trade'} of {ticker}{band} — the ticker is on "
                     "your watchlist. Single trades are noise; clusters matter.",
                symbol=str(ticker), value=float(lo) if lo is not None else None,
            ))
        return out

    def _rule_whale_new_position(self, now: datetime, regime: str) -> list[Alert]:
        """A top-10 holding absent from the fund's previous 13F — one alert
        per (cik, cusip, latest accession), ever."""
        from app.edge.whales import whale_new_positions

        out: list[Alert] = []
        for w in whale_new_positions(self._duck, days=30, top_rank=10):
            key = f"whale_new:{w['cik']}:{w['cusip']}:{w['accession']}"
            if self._fired_ever(key):
                continue
            pc = f" ({w['put_call']})" if w.get("put_call") else ""
            val = f" (~${w['value'] / 1e9:.1f}bn)" if w.get("value") else ""
            out.append(Alert(
                rule="whale_new_position", key=key, severity="info",
                title=f"Whale new position: {w['fund']} → {w['issuer'] or w['cusip']}",
                body=f"{w['fund']}'s latest 13F (quarter ending {w['period']}, "
                     f"filed {w['filed_at']}) shows {w['issuer'] or w['cusip']}{pc} "
                     f"as a NEW top-10 position at rank {w['rank']}{val}. "
                     "13F data lags up to 45 days.",
                symbol=w["issuer"], value=w.get("value"),
            ))
        return out

    # ------------------------------------------------------------------ sweep

    _RULES = (
        _rule_regime_flip, _rule_macro_z, _rule_vix_term, _rule_corr_break,
        _rule_retail_spike, _rule_insider_cluster, _rule_cot_extreme,
        _rule_gex_flip, _rule_large_print,
        _rule_netliq_drain, _rule_congress_watchlist, _rule_whale_new_position,
    )

    def _evaluate(self) -> list[Alert]:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        regime = self._regime()
        fired: list[Alert] = []
        for rule in self._RULES:
            try:
                fired.extend(rule(self, now, regime))
            except Exception as exc:
                log.warning("alert rule %s failed: %s", rule.__name__, exc)
        # Persist + mark state. id hashes rule:key:day so re-sweeps can't dup.
        day = now.strftime("%Y-%m-%d")
        for a in fired:
            aid = hashlib.sha1(f"{a.rule}:{a.key}:{day}".encode()).hexdigest()[:16]
            self._duck.execute(
                "INSERT OR IGNORE INTO alerts "
                "(id, ts, rule, severity, title, body, symbol, value, regime, pushed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE)",
                [aid, now, a.rule, a.severity, a.title, a.body, a.symbol, a.value, regime],
            )
            a.detail["id"] = aid
            self._save_state(a.key, fired=now)
        return fired

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        fired = await loop.run_in_executor(None, self._evaluate)
        for a in fired:
            await self._hub.broadcast(ALERTS_TOPIC, {
                "type": "alert",
                "id": a.detail.get("id"),
                "rule": a.rule,
                "severity": a.severity,
                "title": a.title,
                "body": a.body,
                "symbol": a.symbol,
                "value": a.value,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            if a.severity in ("warn", "critical"):
                pushed = await self._ntfy.publish(
                    a.title, a.body, a.severity,
                    tags="rotating_light" if a.severity == "critical" else "warning",
                )
                if pushed and a.detail.get("id"):
                    await loop.run_in_executor(
                        None, self._duck.execute,
                        "UPDATE alerts SET pushed = TRUE WHERE id = ?",
                        [a.detail["id"]],
                    )
        if fired:
            log.info("alert sweep fired %s alert(s): %s",
                     len(fired), [a.rule for a in fired])
        return len(fired)
