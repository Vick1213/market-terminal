"""Daily pre-market auto-brief (PLAN §6 #1 — the #1 edge-per-effort extra).

Builds a grounded JSON digest from data already in DuckDB (regime, 24h alerts,
cookbook breaks, retail spikes, insider clusters, COT extremes, GEX, upcoming
events) and asks the LOCAL LLM (Qwen3 via Ollama, $0 tokens) for a 30-second
morning read. If Ollama is down the deterministic markdown template renders
the same digest — the brief never silently fails.

Runs on a pre-market cron and on demand via POST /api/brief/run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from app.db.duck import DuckStore
from app.edge.cot import cot_summary
from app.edge.calendar import upcoming_events
from app.edge.ntfy import NtfyPublisher
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.edge.brief")

BRIEF_TOPIC = "brief"

_PROMPT = """You are the morning-brief writer for a personal market terminal.
Below is today's machine-generated digest (JSON). Write a tight, skimmable
pre-market brief in markdown: 120-220 words, 3-5 short sections with bold
mini-headers, no preamble, no disclaimers, no invented numbers — only facts
from the digest. Lead with the regime and what changed in the last 24h, end
with what to watch today/this week (events). If a section has no data, skip it.

DIGEST:
{digest}
"""


def build_digest(duck: DuckStore) -> dict:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    digest: dict = {"generated_at": now.isoformat(timespec="minutes")}

    row = duck.fetchone(
        "SELECT score, regime, ts FROM macro_composite ORDER BY ts DESC LIMIT 1"
    )
    if row:
        digest["regime"] = {"name": row[1], "score": round(float(row[0]), 1),
                            "as_of": str(row[2])[:10]}

    row = duck.fetchone(
        "SELECT detail FROM corr_snapshots WHERE card_id = '_meta' "
        "ORDER BY ts DESC LIMIT 1"
    )
    if row and row[0]:
        meta = json.loads(row[0])
        notable = [
            {"card": c.get("title"), "status": c.get("status"), "z": c.get("z")}
            for c in meta.get("cards", []) if c.get("status") in ("broken", "watch")
        ]
        if notable:
            digest["cookbook"] = notable
        if meta.get("stress", {}).get("on"):
            digest["stress_radar"] = "ON"

    alerts = duck.fetchall(
        "SELECT severity, title FROM alerts WHERE ts >= ? "
        "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, ts DESC "
        "LIMIT 10",
        [now - timedelta(hours=24)],
    )
    if alerts:
        digest["alerts_24h"] = [{"severity": a[0], "title": a[1]} for a in alerts]

    spikes = duck.fetchall(
        """
        SELECT symbol, mentions, mentions_prev FROM ts_retail
        WHERE source = 'apewisdom'
          AND ts = (SELECT max(ts) FROM ts_retail WHERE source = 'apewisdom')
          AND mentions_prev >= 20 AND mentions >= 2 * mentions_prev
        ORDER BY mentions DESC LIMIT 5
        """
    )
    if spikes:
        digest["retail_spikes"] = [
            {"symbol": s[0], "mentions": int(s[1]),
             "vs_24h_ago": f"{s[1] / s[2]:.1f}x"} for s in spikes
        ]

    clusters = duck.fetchall(
        """
        SELECT symbol, count(DISTINCT insider_name), sum(value)
        FROM insider_trades WHERE filed_at >= ?
        GROUP BY symbol HAVING count(DISTINCT insider_name) >= 1
        ORDER BY sum(value) DESC LIMIT 5
        """,
        [now - timedelta(days=14)],
    )
    if clusters:
        digest["insider_buys_14d"] = [
            {"symbol": c[0], "buyers": int(c[1]), "total_usd": round(float(c[2]))}
            for c in clusters
        ]

    extremes = [c for c in cot_summary(duck)
                if c["cot_index"] is not None and (c["cot_index"] >= 90 or c["cot_index"] <= 10)]
    if extremes:
        digest["cot_extremes"] = [
            {"market": c["market"], "cot_index": c["cot_index"]} for c in extremes
        ]

    row = duck.fetchone(
        "SELECT spot, total_gex, flip, call_wall, put_wall FROM gex_snapshots "
        "WHERE symbol = '_SPX' ORDER BY ts DESC LIMIT 1"
    )
    if row and row[0] is not None:
        digest["spx_gamma"] = {
            "spot": round(float(row[0])),
            "total_gex_bn": round(float(row[1]) / 1e9, 1) if row[1] is not None else None,
            "flip": float(row[2]) if row[2] is not None else None,
            "call_wall": float(row[3]) if row[3] is not None else None,
            "put_wall": float(row[4]) if row[4] is not None else None,
        }

    events = [e for e in upcoming_events(duck, days=7)]
    if events:
        digest["events_7d"] = [
            {"in_days": e["days_until"], "title": e["title"]} for e in events[:10]
        ]
    return digest


def render_template(digest: dict) -> str:
    """Deterministic fallback: the same digest as readable markdown."""
    lines: list[str] = []
    reg = digest.get("regime")
    if reg:
        lines.append(f"**Regime:** {str(reg['name']).upper()} "
                     f"(composite {reg['score']:+}, as of {reg['as_of']})")
    if digest.get("stress_radar"):
        lines.append("**Stress radar is ON.**")
    if digest.get("alerts_24h"):
        lines.append("\n**Last 24h alerts**")
        lines += [f"- [{a['severity']}] {a['title']}" for a in digest["alerts_24h"]]
    if digest.get("cookbook"):
        lines.append("\n**Cookbook**")
        lines += [f"- {c['card']}: {str(c['status']).upper()}"
                  + (f" (z {c['z']:+.1f})" if isinstance(c.get("z"), (int, float)) else "")
                  for c in digest["cookbook"]]
    if digest.get("retail_spikes"):
        lines.append("\n**Retail chatter**")
        lines += [f"- {s['symbol']}: {s['mentions']} mentions ({s['vs_24h_ago']})"
                  for s in digest["retail_spikes"]]
    if digest.get("insider_buys_14d"):
        lines.append("\n**Insider buys (14d)**")
        lines += [f"- {c['symbol']}: {c['buyers']} buyer(s), ~${c['total_usd']:,}"
                  for c in digest["insider_buys_14d"]]
    if digest.get("cot_extremes"):
        lines.append("\n**COT extremes**")
        lines += [f"- {c['market']}: index {c['cot_index']}" for c in digest["cot_extremes"]]
    g = digest.get("spx_gamma")
    if g:
        lines.append(
            f"\n**SPX gamma:** spot {g['spot']:,}"
            + (f", flip {g['flip']:,.0f}" if g.get("flip") else "")
            + (f", call wall {g['call_wall']:,.0f}" if g.get("call_wall") else "")
            + (f", put wall {g['put_wall']:,.0f}" if g.get("put_wall") else "")
        )
    if digest.get("events_7d"):
        lines.append("\n**Watch this week**")
        lines += [f"- {'today' if e['in_days'] == 0 else f'in {e['in_days']}d'}: {e['title']}"
                  for e in digest["events_7d"]]
    return "\n".join(lines) if lines else "No data yet — pipelines are still warming up."


class BriefService:
    def __init__(
        self,
        duck: DuckStore,
        hub: ConnectionManager,
        ntfy: NtfyPublisher,
        *,
        ollama_url: str,
        ollama_model: str,
        ollama_timeout: float = 180.0,
    ) -> None:
        self._duck = duck
        self._hub = hub
        self._ntfy = ntfy
        self._ollama_url = ollama_url.rstrip("/")
        self._model = ollama_model
        self._timeout = ollama_timeout

    async def _narrate(self, digest: dict) -> tuple[str, str]:
        """Returns (markdown, model). Falls back to the template on any failure."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._ollama_url}/api/chat",
                    json={
                        "model": self._model,
                        "messages": [{
                            "role": "user",
                            "content": _PROMPT.format(
                                digest=json.dumps(digest, indent=1)),
                        }],
                        "stream": False,
                        "options": {"temperature": 0.3},
                    },
                )
                resp.raise_for_status()
                text = resp.json()["message"]["content"]
            # Qwen3 thinking models wrap reasoning in <think> — strip it.
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            if len(text) < 40:
                raise ValueError("LLM returned an empty/too-short brief")
            return text, self._model
        except Exception as exc:
            log.warning("ollama brief failed (%s) — using template fallback", exc)
            return render_template(digest), "template"

    async def run(self, push: bool = True) -> dict:
        loop = asyncio.get_running_loop()
        digest = await loop.run_in_executor(None, build_digest, self._duck)
        text, model = await self._narrate(digest)

        now = datetime.now(timezone.utc)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        regime = (digest.get("regime") or {}).get("name", "unknown")
        await loop.run_in_executor(
            None, self._duck.execute,
            "INSERT OR REPLACE INTO briefs (ts, regime, text, model, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            [day, regime, text, model, json.dumps(digest)],
        )
        await self._hub.broadcast(BRIEF_TOPIC, {
            "type": "brief", "date": day.strftime("%Y-%m-%d"),
            "regime": regime, "model": model,
        })
        if push:
            await self._ntfy.publish(
                f"Morning brief — {regime}", text[:600], "info", tags="newspaper",
            )
        log.info("brief generated for %s (model=%s, %s chars)",
                 day.date(), model, len(text))
        return {"date": day.strftime("%Y-%m-%d"), "regime": regime,
                "model": model, "text": text, "digest": digest}
