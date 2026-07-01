"""Persist each learning-loop review as a human-readable markdown file.

The day/swing review loops already write a row to SQLite (day_review /
swing_review) and return a ``result`` dict. That's great for the UI but awful
for a human who just wants to skim "what did the bot learn and where should I
look". This module renders that same result into a markdown file under
``<data_dir>/learnings/`` so the findings, suggested parameter tweaks, and the
win-rate tables are readable at a glance and diffable across runs.

Two files are written per review:
  * ``<sleeve>-<date>.md``  — the dated snapshot (history)
  * ``<sleeve>-latest.md``  — overwritten each run (the "current" view)

Best-effort: any failure is logged and swallowed — never breaks a review.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("market.trading.learnings")

_SEV_MARK = {"critical": "🔴", "warn": "🟡", "info": "⚪"}


def _fmt_money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"


def _fmt_pct(v) -> str:
    try:
        return f"{float(v) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _leg_table(title: str, legs: dict | None) -> list[str]:
    """Render a {key: {n, wins, losses, win_rate, total_pnl, avg_pnl}} group as a
    markdown table, worst-P&L first. Skips empty / all-'none' groups."""
    if not isinstance(legs, dict):
        return []
    rows = [(k, v) for k, v in legs.items()
            if k != "none" and isinstance(v, dict)
            and (int(v.get("wins", 0)) + int(v.get("losses", 0))) > 0]
    if not rows:
        return []
    rows.sort(key=lambda kv: (kv[1].get("total_pnl") or 0.0))
    out = [f"**{title}**", "", "| bucket | n | win% | net P&L | avg |", "|---|--:|--:|--:|--:|"]
    for k, v in rows:
        n = int(v.get("n", (v.get("wins", 0) + v.get("losses", 0))))
        out.append(f"| {k} | {n} | {_fmt_pct(v.get('win_rate'))} | "
                   f"{_fmt_money(v.get('total_pnl'))} | {_fmt_money(v.get('avg_pnl'))} |")
    out.append("")
    return out


def render_markdown(result: dict, *, sleeve: str) -> str:
    """Render one review ``result`` dict into a markdown document."""
    date = result.get("trade_date") or result.get("review_date") or "?"
    stats = result.get("stats") or {}
    findings = [f for f in (result.get("findings") or []) if f.get("tag") != "none"]
    suggestions = result.get("suggestions") or []
    label = "Day trader" if sleeve == "day" else "Swing"

    L: list[str] = []
    L.append(f"# {label} learnings — {date}")
    L.append("")
    L.append(f"_generated {result.get('created_at', '?')} · model: {result.get('model', '?')}_")
    L.append("")

    if result.get("summary"):
        L.append("## Summary")
        L.append("")
        L.append(str(result["summary"]))
        L.append("")

    # Headline numbers (shape differs by sleeve — render whatever is present).
    overall = stats.get("overall") if isinstance(stats.get("overall"), dict) else None
    headline: list[str] = []
    if overall:
        headline.append(f"- closed: **{stats.get('n_closed', '?')}** · "
                        f"win {_fmt_pct(overall.get('win_rate'))} · "
                        f"net {_fmt_money(overall.get('total_pnl'))}")
        if stats.get("n_open"):
            headline.append(f"- open: **{stats['n_open']}** · marked {_fmt_money(stats.get('open_pnl'))}")
    rr = stats.get("risk_reward")
    if isinstance(rr, dict) and rr.get("reward_risk_usd") is not None:
        headline.append(
            f"- realized reward:risk **{rr.get('reward_risk_usd')}** by $ "
            f"({rr.get('reward_risk_pct')} by %) · avg win {_fmt_money(rr.get('avg_win_usd'))} "
            f"vs avg loss {_fmt_money(rr.get('avg_loss_usd'))} "
            f"({rr.get('n_wins', '?')}W / {rr.get('n_losses', '?')}L)")
    sh = stats.get("sharpe")
    if isinstance(sh, dict) and sh.get("per_trade") is not None:
        headline.append(f"- Sharpe: per-trade **{sh.get('per_trade')}** (n={sh.get('n_trades', '?')})"
                        + (f" · daily {sh.get('daily')}" if sh.get("daily") is not None else ""))
    if headline:
        L.append("## Headline")
        L.append("")
        L.extend(headline)
        L.append("")

    # Findings — the "where to look" list.
    L.append("## Findings")
    L.append("")
    if findings:
        for f in findings:
            mark = _SEV_MARK.get(f.get("severity", "info"), "•")
            L.append(f"- {mark} **{f.get('title', '?')}** — {f.get('detail', '')}")
    else:
        L.append("_no findings this run._")
    L.append("")

    # Suggested parameter tweaks — the "where to improve" list.
    L.append("## Suggested parameter tweaks")
    L.append("")
    if suggestions:
        for s in suggestions:
            tag = "advisory" if s.get("actionable") is False else "ready"
            conf = s.get("confidence")
            extra = f" · {conf}" if conf else ""
            n = s.get("n")
            extra += f" · n={n}" if n is not None else ""
            L.append(f"- `{s.get('param', '?')}`: **{s.get('current')} → {s.get('proposed')}** "
                     f"({tag}{extra})")
            if s.get("rationale"):
                L.append(f"  - {s['rationale']}")
    else:
        L.append("_no parameter suggestions this run._")
    L.append("")

    # Win-rate tables — every "by_*" group the review computed.
    tables: list[str] = []
    for key, val in stats.items():
        if not key.startswith("by_"):
            continue
        pretty = key[3:].replace("_", " ")
        tables.extend(_leg_table(f"By {pretty}", val))
    if tables:
        L.append("## Breakdown")
        L.append("")
        L.extend(tables)

    return "\n".join(L).rstrip() + "\n"


def write_learnings_file(result: dict, *, sleeve: str, data_dir: Path) -> Path | None:
    """Write the review as ``<sleeve>-<date>.md`` plus ``<sleeve>-latest.md``
    under ``<data_dir>/learnings/``. Returns the dated path, or None on failure.
    Never raises."""
    try:
        date = result.get("trade_date") or result.get("review_date") or "unknown"
        out_dir = Path(data_dir) / "learnings"
        out_dir.mkdir(parents=True, exist_ok=True)
        md = render_markdown(result, sleeve=sleeve)
        dated = out_dir / f"{sleeve}-{date}.md"
        dated.write_text(md, encoding="utf-8")
        (out_dir / f"{sleeve}-latest.md").write_text(md, encoding="utf-8")
        log.info("learnings written: %s", dated)
        return dated
    except Exception:
        log.warning("failed to write %s learnings file", sleeve, exc_info=True)
        return None
