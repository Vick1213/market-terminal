"use client";

import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import type { NewsItem } from "@market/shared";
import { fetchNews } from "@/lib/api";
import { EdgarChips, OutletBadge, sentColor } from "./NewsPanel";

const ROLL_WIN = 7; // rolling mean/std window (items)

/**
 * Expanded sentiment chart: every scored item as a time-positioned dot
 * (color = sign, opacity = confidence), rolling mean line, and ±1σ dispersion
 * band — PLAN §3a: track velocity AND dispersion, a spike in cross-article
 * disagreement is often more predictive than the level.
 */
function BigSentimentChart({ items }: { items: NewsItem[] }) {
  const scored = useMemo(
    () =>
      items
        .filter((i) => i.score !== null)
        .sort((a, b) => a.published.localeCompare(b.published)),
    [items]
  );

  if (scored.length < 2) {
    return <div className="chart-empty">not enough scored items to chart</div>;
  }

  const W = 900;
  const H = 280;
  const padL = 34;
  const padR = 12;
  const padT = 10;
  const padB = 22;

  const times = scored.map((i) => new Date(i.published).getTime());
  const t0 = times[0];
  const t1 = times[times.length - 1];
  const span = Math.max(t1 - t0, 1);
  const x = (t: number) => padL + ((t - t0) / span) * (W - padL - padR);
  const y = (v: number) => padT + (1 - (v + 1) / 2) * (H - padT - padB);

  // Rolling mean ± std at each scored item.
  const roll = scored.map((_, idx) => {
    const slice = scored.slice(Math.max(0, idx - ROLL_WIN + 1), idx + 1);
    const vals = slice.map((i) => i.score as number);
    const mean = vals.reduce((s, v) => s + v, 0) / vals.length;
    const std =
      vals.length > 1
        ? Math.sqrt(vals.reduce((s, v) => s + (v - mean) ** 2, 0) / (vals.length - 1))
        : 0;
    return { t: times[idx], mean, std };
  });

  const meanPath = roll
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.mean).toFixed(1)}`)
    .join(" ");
  const bandPath =
    roll
      .map(
        (p, i) =>
          `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(Math.min(p.mean + p.std, 1)).toFixed(1)}`
      )
      .join(" ") +
    " " +
    [...roll]
      .reverse()
      .map((p) => `L${x(p.t).toFixed(1)},${y(Math.max(p.mean - p.std, -1)).toFixed(1)}`)
      .join(" ") +
    " Z";

  // Time ticks: 5 evenly spaced; include date when the window spans >24h.
  const multiDay = span > 24 * 3600 * 1000;
  const ticks = Array.from({ length: 5 }, (_, i) => t0 + (span * i) / 4);
  const fmt = (t: number) => {
    const d = new Date(t);
    const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return multiDay
      ? `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${hm}`
      : hm;
  };

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="big-chart"
      preserveAspectRatio="none"
      aria-label="sentiment over time"
    >
      {/* gridlines at -1 / 0 / +1 */}
      {[-1, 0, 1].map((v) => (
        <g key={v}>
          <line
            x1={padL}
            y1={y(v)}
            x2={W - padR}
            y2={y(v)}
            stroke="var(--panel-border)"
            strokeWidth={v === 0 ? 1.5 : 1}
            strokeDasharray={v === 0 ? undefined : "3 4"}
          />
          <text x={4} y={y(v) + 4} className="chart-label">
            {v > 0 ? "+1" : v}
          </text>
        </g>
      ))}
      {ticks.map((t, i) => (
        <text
          key={i}
          x={x(t)}
          y={H - 6}
          className="chart-label"
          textAnchor={i === 0 ? "start" : i === ticks.length - 1 ? "end" : "middle"}
        >
          {fmt(t)}
        </text>
      ))}

      <path d={bandPath} fill="rgba(41, 98, 255, 0.10)" stroke="none" />
      <path d={meanPath} fill="none" stroke="var(--accent)" strokeWidth={1.5} />

      {scored.map((i, idx) => (
        <circle
          key={i.id}
          cx={x(times[idx])}
          cy={y(i.score as number)}
          r={3.5}
          fill={sentColor(i.score)}
          opacity={0.35 + 0.65 * (i.confidence ?? 0.5)}
        >
          <title>
            {`${i.title}\n${i.label ?? "?"} ${(i.score as number).toFixed(2)} (conf ${
              i.confidence?.toFixed(2) ?? "?"
            }) · ${i.source}${i.symbol ? ` · ${i.symbol}` : ""}`}
          </title>
        </circle>
      ))}
    </svg>
  );
}

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
            <BigSentimentChart items={items} />
            <div className="chart-legend">
              <span>
                <i className="legend-dot" style={{ background: "var(--green)" }} /> positive
              </span>
              <span>
                <i className="legend-dot" style={{ background: "var(--red)" }} /> negative
              </span>
              <span>
                <i className="legend-dot" style={{ background: "var(--text-dim)" }} /> neutral
              </span>
              <span>
                <i className="legend-line" /> rolling mean ({ROLL_WIN})
              </span>
              <span>
                <i className="legend-band" /> ±1σ dispersion
              </span>
            </div>
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
