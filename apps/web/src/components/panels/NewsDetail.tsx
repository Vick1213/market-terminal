"use client";

import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import type { NewsItem } from "@market/shared";
import { fetchNews } from "@/lib/api";
import { MarketChart } from "../charts/MarketChart";
import { EdgarChips, OutletBadge, sentColor } from "./NewsPanel";

const ROLL_WIN = 7; // rolling mean/std window (items)

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="stat">
      <div className="stat-value" style={color ? { color } : undefined}>
        {value}
      </div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export function NewsDetail({
  initialSymbol,
  liveItems,
  onClose,
}: {
  initialSymbol: string | null;
  liveItems: NewsItem[];
  onClose: () => void;
}) {
  const [symbol, setSymbol] = useState<string | null>(initialSymbol);
  const [source, setSource] = useState<string | null>(null);

  // Deeper backfill than the small panel: 500 items.
  const { data, isLoading } = useQuery({
    queryKey: ["news-detail", symbol],
    queryFn: () => fetchNews(symbol ?? undefined, 500),
    refetchInterval: 60_000,
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const items = useMemo(() => {
    const byId = new Map<string, NewsItem>();
    for (const i of [...liveItems, ...(data?.items ?? [])]) byId.set(i.id, i);
    return [...byId.values()]
      .filter((i) => (!symbol || i.symbol === symbol) && (!source || i.source === source))
      .sort((a, b) => b.published.localeCompare(a.published));
  }, [liveItems, data, symbol, source]);

  const symbols = data?.symbols ?? [];
  const sources = useMemo(
    () => [...new Set(items.map((i) => i.source))].sort(),
    // Source chips from the symbol-filtered (not source-filtered) set, so a
    // selected source chip doesn't make the others disappear.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data, liveItems, symbol]
  );

  // --- summary stats over the filtered window ---
  const stats = useMemo(() => {
    const scored = items
      .filter((i) => i.score !== null)
      .sort((a, b) => a.published.localeCompare(b.published));
    const vals = scored.map((i) => i.score as number);
    const n = vals.length;
    const mean = n ? vals.reduce((s, v) => s + v, 0) / n : 0;
    const std =
      n > 1 ? Math.sqrt(vals.reduce((s, v) => s + (v - mean) ** 2, 0) / (n - 1)) : 0;
    // velocity: mean of newest ROLL_WIN minus mean of the ROLL_WIN before it
    const recent = vals.slice(-ROLL_WIN);
    const prior = vals.slice(-2 * ROLL_WIN, -ROLL_WIN);
    const avg = (a: number[]) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : 0);
    const velocity = prior.length ? avg(recent) - avg(prior) : 0;
    const byLabel = (l: string) => items.filter((i) => i.label === l).length;
    return {
      n: items.length,
      scored: n,
      mean,
      std,
      velocity,
      pos: byLabel("positive"),
      neg: byLabel("negative"),
      neu: byLabel("neutral"),
      confirmed: items.filter((i) => (i.outlets ?? 1) >= 2).length,
    };
  }, [items]);

  const modal = (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>
            News + Sentiment — detail
            {symbol ? ` · ${symbol}` : " · all symbols"}
          </span>
          <button className="expand-btn" title="Close (Esc)" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          <div className="chip-row">
            <button className={`chip ${symbol === null ? "active" : ""}`} onClick={() => setSymbol(null)}>
              ALL
            </button>
            {symbols.map((s) => (
              <button
                key={s}
                className={`chip ${symbol === s ? "active" : ""}`}
                onClick={() => setSymbol(symbol === s ? null : s)}
              >
                {s}
              </button>
            ))}
            <span className="chip-sep" />
            {sources.map((s) => (
              <button
                key={s}
                className={`chip ${source === s ? "active" : ""}`}
                onClick={() => setSource(source === s ? null : s)}
              >
                {s}
              </button>
            ))}
          </div>

          <div className="stat-row">
            <Stat label="items" value={String(stats.n)} />
            <Stat
              label="avg sentiment"
              value={`${stats.mean > 0 ? "+" : ""}${stats.mean.toFixed(2)}`}
              color={sentColor(stats.mean)}
            />
            <Stat
              label={`velocity (Δ last ${ROLL_WIN})`}
              value={`${stats.velocity > 0 ? "+" : ""}${stats.velocity.toFixed(2)}`}
              color={sentColor(stats.velocity)}
            />
            <Stat label="dispersion (σ)" value={stats.std.toFixed(2)} />
            <Stat label="pos / neu / neg" value={`${stats.pos} / ${stats.neu} / ${stats.neg}`} />
            <Stat
              label="multi-outlet"
              value={String(stats.confirmed)}
              color={stats.confirmed > 0 ? "var(--yellow)" : undefined}
            />
          </div>

          <div className="chart-wrap">
            <MarketChart
              key={symbol ?? "ALL"}
              chartKey={`news:${symbol ?? "ALL"}`}
              initialSeriesIds={[`SENT:${symbol ?? "ALL"}`]}
              days={30}
              height={300}
            />
          </div>

          {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}

          <ul className="news-list detail-list">
            {items.map((i) => (
              <li key={i.id} className="detail-item">
                <div className="detail-item-head">
                  <span
                    className="sent-dot"
                    style={{
                      background: sentColor(i.score),
                      opacity: 0.35 + 0.65 * (i.confidence ?? 0.5),
                    }}
                  />
                  <span className="news-meta">
                    {new Date(i.published).toLocaleString([], {
                      month: "short",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                    {" · "}
                    {i.source}
                    {i.symbol ? ` · ${i.symbol}` : ""}
                    <OutletBadge item={i} />
                  </span>
                  {i.url ? (
                    <a className="news-title" href={i.url} target="_blank" rel="noreferrer">
                      {i.title}
                    </a>
                  ) : (
                    <span className="news-title">{i.title}</span>
                  )}
                  <EdgarChips item={i} alertOnly={false} />
                  {i.score !== null && (
                    <span className="news-score" style={{ color: sentColor(i.score) }}>
                      {i.score > 0 ? "+" : ""}
                      {i.score.toFixed(2)}
                      <span className="conf-note"> conf {i.confidence?.toFixed(2) ?? "?"}</span>
                    </span>
                  )}
                </div>
                {i.summary && <div className="detail-summary">{i.summary}</div>}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );

  // Portal to <body>: grid items carry CSS transforms, which would re-anchor
  // position:fixed and clip the overlay.
  return createPortal(modal, document.body);
}
