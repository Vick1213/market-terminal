"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { NewsItem, NewsMessage } from "@market/shared";
import { fetchNews } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";
import { NewsDetail } from "./NewsDetail";

/** Sentiment color: green/red by sign, dim for the (explicit) neutral band. */
export function sentColor(score: number | null): string {
  if (score === null || Math.abs(score) < 0.15) return "var(--text-dim)";
  return score > 0 ? "var(--green)" : "var(--red)";
}

// --- EDGAR 8-K alert chips (PLAN §3a: Item 1.05 / 2.02 / 8.01) ---

const EDGAR_ITEM_NAMES: Record<string, string> = {
  "1.01": "material agreement",
  "1.05": "cyber incident",
  "2.02": "results",
  "5.02": "exec change",
  "7.01": "reg FD",
  "8.01": "other events",
};
const EDGAR_ALERT_ITEMS = new Set(["1.05", "2.02", "8.01"]);

export function edgarItemCodes(item: NewsItem): string[] {
  if (item.source !== "edgar") return [];
  const m = item.title.match(/\(items ([^)]+)\)/i);
  return m ? m[1].split(",").map((s) => s.trim()) : [];
}

export function EdgarChips({ item, alertOnly = true }: { item: NewsItem; alertOnly?: boolean }) {
  const codes = edgarItemCodes(item).filter(
    (c) => EDGAR_ITEM_NAMES[c] && (!alertOnly || EDGAR_ALERT_ITEMS.has(c))
  );
  if (codes.length === 0) return null;
  return (
    <>
      {codes.map((c) => (
        <span
          key={c}
          className={`filing-chip ${c === "1.05" ? "danger" : EDGAR_ALERT_ITEMS.has(c) ? "alert" : ""}`}
          title={`8-K item ${c}`}
        >
          {c} {EDGAR_ITEM_NAMES[c]}
        </span>
      ))}
    </>
  );
}

/** Multi-outlet convergence badge: >=2 independent outlets on one deduped story. */
export function OutletBadge({ item }: { item: NewsItem }) {
  const n = item.outlets ?? 1;
  if (n < 2) return null;
  return (
    <span className="outlet-badge" title={`Confirmed by ${n} outlets: ${item.outlet_names ?? ""}`}>
      ×{n}
    </span>
  );
}

/**
 * Sentiment-velocity sparkline: rolling mean (window 5) of scores in published
 * order — PLAN §3a says track velocity, not just level.
 */
function Sparkline({ items }: { items: NewsItem[] }) {
  const points = useMemo(() => {
    const scored = items
      .filter((i) => i.score !== null)
      .sort((a, b) => a.published.localeCompare(b.published));
    const win = 5;
    return scored.map((_, idx) => {
      const slice = scored.slice(Math.max(0, idx - win + 1), idx + 1);
      return slice.reduce((s, i) => s + (i.score ?? 0), 0) / slice.length;
    });
  }, [items]);

  if (points.length < 2) return null;

  const w = 120;
  const h = 24;
  const path = points
    .map((v, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h / 2 - v * (h / 2 - 2); // -1..+1 -> bottom..top
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const last = points[points.length - 1];

  return (
    <svg width={w} height={h} className="sparkline" aria-label="sentiment velocity">
      <line x1={0} y1={h / 2} x2={w} y2={h / 2} stroke="var(--panel-border)" strokeWidth={1} />
      <polyline points={path} fill="none" stroke={sentColor(last)} strokeWidth={1.5} />
    </svg>
  );
}

export function NewsPanel() {
  const [symbol, setSymbol] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["news", symbol],
    queryFn: () => fetchNews(symbol ?? undefined),
    refetchInterval: 60_000,
  });

  const { status, messages } = useWebSocket("news", 100);

  // All WS-pushed items, unfiltered — the detail view does its own filtering.
  const liveItems = useMemo(
    () =>
      messages
        .filter((m): m is NewsMessage => (m as NewsMessage).type === "news")
        .map((m) => m.item),
    [messages]
  );

  // Merge WS-pushed items over the REST backfill, dedupe by id, newest first.
  const items = useMemo(() => {
    const live = liveItems.filter((i) => !symbol || i.symbol === symbol);
    const byId = new Map<string, NewsItem>();
    for (const i of [...live, ...(data?.items ?? [])]) byId.set(i.id, i);
    return [...byId.values()].sort((a, b) => b.published.localeCompare(a.published));
  }, [liveItems, data, symbol]);

  const chips = data?.symbols ?? [];

  // Expand on any click in the panel that isn't a link / button / chip.
  const onPanelClick = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest("a, button")) return;
    setExpanded(true);
  };

  return (
    <div className="panel panel-expandable" onClick={onPanelClick} title="Click to expand">
      <div className="panel-head">
        <span>News + Sentiment</span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Sparkline items={items} />
          <span className={`badge ${status === "open" ? "live" : "closed"}`}>
            {status === "open" ? "WS live" : status}
          </span>
          <button
            className="expand-btn"
            title="Expand for full sentiment analysis"
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(true);
            }}
          >
            ⤢
          </button>
        </span>
      </div>
      <div className="panel-body">
        {chips.length > 0 && (
          <div className="chip-row">
            <button
              className={`chip ${symbol === null ? "active" : ""}`}
              onClick={() => setSymbol(null)}
            >
              ALL
            </button>
            {chips.map((s) => (
              <button
                key={s}
                className={`chip ${symbol === s ? "active" : ""}`}
                onClick={() => setSymbol(symbol === s ? null : s)}
              >
                {s}
              </button>
            ))}
          </div>
        )}

        {isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}
        {!isLoading && items.length === 0 && (
          <div style={{ color: "var(--text-dim)" }}>
            no items yet — ingestors run shortly after boot, then every ~10 min
          </div>
        )}

        <ul className="news-list">
          {items.map((i) => (
            <li key={i.id} className="news-item">
              <span
                className="sent-dot"
                title={`${i.label ?? "?"} ${i.score?.toFixed(2) ?? ""} (conf ${i.confidence?.toFixed(2) ?? "?"})`}
                style={{
                  background: sentColor(i.score),
                  opacity: 0.35 + 0.65 * (i.confidence ?? 0.5),
                }}
              />
              <span className="news-meta">
                {new Date(i.published).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
                {" · "}
                {i.source}
                {i.symbol ? ` · ${i.symbol}` : ""}
                <OutletBadge item={i} />
              </span>
              <span style={{ display: "flex", alignItems: "baseline", gap: 4, minWidth: 0 }}>
                {i.url ? (
                  <a className="news-title" href={i.url} target="_blank" rel="noreferrer">
                    {i.title}
                  </a>
                ) : (
                  <span className="news-title">{i.title}</span>
                )}
                <EdgarChips item={i} />
              </span>
              {i.score !== null && (
                <span className="news-score" style={{ color: sentColor(i.score) }}>
                  {i.score > 0 ? "+" : ""}
                  {i.score.toFixed(2)}
                </span>
              )}
            </li>
          ))}
        </ul>
      </div>

      {expanded && (
        <NewsDetail
          initialSymbol={symbol}
          liveItems={liveItems}
          onClose={() => setExpanded(false)}
        />
      )}
    </div>
  );
}
