"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { WatchlistQuote } from "@market/shared";
import {
  addWatchlistSymbol,
  fetchNews,
  fetchWatchlist,
  removeWatchlistSymbol,
} from "@/lib/api";
import { MarketChart } from "../charts/MarketChart";
import { sentColor } from "./NewsPanel";

const ASSET_CLASSES = ["equity", "crypto", "metal", "fx", "future"] as const;

function fmtPrice(v: number | null): string {
  if (v === null) return "—";
  if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return v.toFixed(2);
}

function fmtPct(v: number | null): string {
  if (v === null) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function fmtVolume(v: number | null): string {
  if (v === null || v === 0) return "—";
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return String(Math.round(v));
}

function changeColor(v: number | null): string {
  if (v === null) return "var(--text-dim)";
  return v >= 0 ? "var(--green)" : "var(--red)";
}

/** Per-row mini price sparkline over the last ~30 daily closes. */
function PriceSpark({ q }: { q: WatchlistQuote }) {
  const pts = q.spark;
  if (pts.length < 2) return <span className="wl-spark" />;
  const w = 64;
  const h = 18;
  const min = Math.min(...pts);
  const max = Math.max(...pts);
  const span = max - min || 1;
  const path = pts
    .map((v, i) => {
      const x = (i / (pts.length - 1)) * w;
      const y = h - 2 - ((v - min) / span) * (h - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={w} height={h} className="wl-spark" aria-label={`${q.symbol} 30d closes`}>
      <polyline
        points={path}
        fill="none"
        stroke={pts[pts.length - 1] >= pts[0] ? "var(--green)" : "var(--red)"}
        strokeWidth={1.2}
      />
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

/** Per-symbol drill-down: stats + the shared composable chart + its news. */
function SymbolDetail({ q, onClose }: { q: WatchlistQuote; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const { data: news } = useQuery({
    queryKey: ["news", q.symbol],
    queryFn: () => fetchNews(q.symbol),
    refetchInterval: 60_000,
  });
  const items = (news?.items ?? []).slice(0, 12);

  const modal = (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>
            {q.symbol}
            {q.display_name && (
              <span style={{ color: "var(--text-dim)", marginLeft: 8, fontWeight: 400 }}>
                {q.display_name}
              </span>
            )}
            <span className="filing-chip" style={{ marginLeft: 10 }}>
              {q.asset_class}
            </span>
          </span>
          <button className="expand-btn" title="Close (Esc)" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          <div className="stat-row">
            <Stat label="last close" value={fmtPrice(q.close)} />
            <Stat label="day change" value={fmtPct(q.change_pct)} color={changeColor(q.change_pct)} />
            <Stat label="day range" value={`${fmtPrice(q.low)} – ${fmtPrice(q.high)}`} />
            <Stat label="volume" value={fmtVolume(q.volume)} />
            <Stat label="as of (EOD)" value={q.ts ? q.ts.slice(0, 10) : "—"} />
          </div>

          <div className="chart-wrap">
            <MarketChart
              chartKey={`watchlist-${q.symbol}`}
              initialSeriesIds={[`PRICE:${q.symbol}`]}
              days={365}
              height={360}
            />
          </div>

          <div className="macro-detail-title">Latest headlines</div>
          {items.length === 0 && (
            <div style={{ color: "var(--text-dim)", fontSize: 12 }}>
              no scored news for {q.symbol} yet
            </div>
          )}
          <ul className="news-list">
            {items.map((i) => (
              <li key={i.id} className="news-item">
                <span className="sent-dot" style={{ background: sentColor(i.score) }} />
                <span className="news-meta">
                  {new Date(i.published).toLocaleDateString([], { month: "short", day: "numeric" })}
                  {" · "}
                  {i.source}
                </span>
                {i.url ? (
                  <a className="news-title" href={i.url} target="_blank" rel="noreferrer">
                    {i.title}
                  </a>
                ) : (
                  <span className="news-title">{i.title}</span>
                )}
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
      </div>
    </div>
  );
  return createPortal(modal, document.body);
}

/** Expanded view: full-width table with OHLC/volume/headline + add/remove. */
function WatchlistDetail({
  quotes,
  onPick,
  onClose,
}: {
  quotes: WatchlistQuote[];
  onPick: (q: WatchlistQuote) => void;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [symbol, setSymbol] = useState("");
  const [assetClass, setAssetClass] = useState<string>("equity");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    // New/removed symbols change the chart picker's price group too.
    queryClient.invalidateQueries({ queryKey: ["series-catalog"] });
  };
  const add = useMutation({
    mutationFn: addWatchlistSymbol,
    onSuccess: () => {
      setSymbol("");
      invalidate();
    },
  });
  const remove = useMutation({ mutationFn: removeWatchlistSymbol, onSuccess: invalidate });

  const modal = (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>
            Custom Watchlist — detail
            <span className="conf-note" style={{ marginLeft: 8 }}>
              daily closes · equity prices delayed
            </span>
          </span>
          <button className="expand-btn" title="Close (Esc)" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          <form
            className="wl-add"
            onSubmit={(e) => {
              e.preventDefault();
              if (symbol.trim() && !add.isPending) {
                add.mutate({ symbol: symbol.trim().toUpperCase(), asset_class: assetClass });
              }
            }}
          >
            <input
              className="wl-add-input"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              placeholder="add symbol — e.g. TSLA, BTC/USD, DX-Y.NYB"
              spellCheck={false}
            />
            <select
              className="wl-add-select"
              value={assetClass}
              onChange={(e) => setAssetClass(e.target.value)}
            >
              {ASSET_CLASSES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
            <button className="expand-btn" type="submit" disabled={add.isPending}>
              {add.isPending ? "validating…" : "+ add"}
            </button>
            {add.isError && (
              <span style={{ color: "var(--red)", fontSize: 11 }}>
                {(add.error as Error).message}
              </span>
            )}
          </form>

          <table className="wl-table wl-table-full">
            <thead>
              <tr>
                <th>symbol</th>
                <th className="num">last</th>
                <th className="num">chg%</th>
                <th className="num">open</th>
                <th className="num">high</th>
                <th className="num">low</th>
                <th className="num">vol</th>
                <th>as of</th>
                <th>latest headline</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {quotes.map((q) => (
                <tr key={q.symbol} className="wl-row" onClick={() => onPick(q)}>
                  <td>
                    <span className="wl-sym">{q.symbol}</span>
                    {q.display_name && <span className="wl-name"> {q.display_name}</span>}
                  </td>
                  <td className="num">{fmtPrice(q.close)}</td>
                  <td className="num" style={{ color: changeColor(q.change_pct) }}>
                    {fmtPct(q.change_pct)}
                  </td>
                  <td className="num">{fmtPrice(q.open)}</td>
                  <td className="num">{fmtPrice(q.high)}</td>
                  <td className="num">{fmtPrice(q.low)}</td>
                  <td className="num">{fmtVolume(q.volume)}</td>
                  <td style={{ color: "var(--text-dim)" }}>{q.ts ? q.ts.slice(0, 10) : "—"}</td>
                  <td className="wl-headline">
                    {q.sent_title ? (
                      <>
                        <span className="sent-dot" style={{ background: sentColor(q.sent_score) }} />
                        {q.sent_title}
                      </>
                    ) : (
                      <span style={{ color: "var(--text-dim)" }}>—</span>
                    )}
                  </td>
                  <td>
                    <button
                      className="wl-remove"
                      title={`Remove ${q.symbol}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        remove.mutate(q.symbol);
                      }}
                    >
                      ✕
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
  return createPortal(modal, document.body);
}

type SortKey = "order" | "symbol" | "change" | "sent";

export function WatchlistPanel() {
  const [detail, setDetail] = useState<WatchlistQuote | null>(null);
  const [managing, setManaging] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("order");
  const [sortDesc, setSortDesc] = useState(true);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["watchlist"],
    queryFn: fetchWatchlist,
    refetchInterval: 90_000, // PLAN §3d: 60–120 s, cache-first server side
  });

  const quotes = useMemo(() => {
    const qs = [...(data?.quotes ?? [])];
    if (sortKey === "order") return qs; // server sort_order
    const dir = sortDesc ? -1 : 1;
    qs.sort((a, b) => {
      if (sortKey === "symbol") return dir * b.symbol.localeCompare(a.symbol);
      if (sortKey === "change") return dir * ((a.change_pct ?? -Infinity) < (b.change_pct ?? -Infinity) ? -1 : 1);
      return dir * ((a.sent_score ?? -Infinity) < (b.sent_score ?? -Infinity) ? -1 : 1);
    });
    return qs;
  }, [data, sortKey, sortDesc]);

  const onSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDesc((d) => !d);
    } else {
      setSortKey(key);
      setSortDesc(key !== "symbol");
    }
  };
  const arrow = (key: SortKey) => (sortKey === key ? (sortDesc ? " ↓" : " ↑") : "");

  // Expand on a TRUE click only — same drag-safe pattern as the other panels;
  // row clicks open the symbol drill-down instead.
  const downAt = useRef<{ x: number; y: number } | null>(null);
  const onPanelMouseDown = (e: React.MouseEvent) => {
    downAt.current = { x: e.clientX, y: e.clientY };
  };
  const onPanelClick = (e: React.MouseEvent) => {
    const d = downAt.current;
    const dragged = d && Math.hypot(e.clientX - d.x, e.clientY - d.y) > 5;
    downAt.current = null;
    if (dragged) return;
    if ((e.target as HTMLElement).closest("a, button, select, input, .wl-row, th")) return;
    setManaging(true);
  };

  return (
    <div
      className="panel panel-expandable"
      onMouseDownCapture={onPanelMouseDown}
      onClick={onPanelClick}
      title="Click to expand"
    >
      <div className="panel-head">
        <span>Custom Watchlist</span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="badge soon" title="Daily closes from cached EOD history; equities are delayed">
            EOD
          </span>
          <button
            className="expand-btn"
            title="Expand: full quote table + add/remove symbols"
            onClick={(e) => {
              e.stopPropagation();
              setManaging(true);
            }}
          >
            ⤢
          </button>
        </span>
      </div>
      <div className="panel-body">
        {isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {isLoading && (
          <div style={{ color: "var(--text-dim)" }}>loading… first load fetches price history</div>
        )}

        {quotes.length > 0 && (
          <table className="wl-table">
            <thead>
              <tr>
                <th onClick={() => onSort("symbol")}>symbol{arrow("symbol")}</th>
                <th className="num">last</th>
                <th className="num" onClick={() => onSort("change")}>
                  chg%{arrow("change")}
                </th>
                <th className="num">30d</th>
                <th className="num" onClick={() => onSort("sent")}>
                  sent{arrow("sent")}
                </th>
              </tr>
            </thead>
            <tbody>
              {quotes.map((q) => (
                <tr
                  key={q.symbol}
                  className="wl-row"
                  title={q.sent_title ?? `${q.symbol} — click for chart + news`}
                  onClick={(e) => {
                    e.stopPropagation();
                    setDetail(q);
                  }}
                >
                  <td>
                    <span className="wl-sym">{q.symbol}</span>
                  </td>
                  <td className="num">{fmtPrice(q.close)}</td>
                  <td className="num" style={{ color: changeColor(q.change_pct) }}>
                    {fmtPct(q.change_pct)}
                  </td>
                  <td className="num">
                    <PriceSpark q={q} />
                  </td>
                  <td className="num">
                    {q.sent_score !== null ? (
                      <span style={{ color: sentColor(q.sent_score) }}>
                        {q.sent_score > 0 ? "+" : ""}
                        {q.sent_score.toFixed(2)}
                      </span>
                    ) : (
                      <span style={{ color: "var(--text-dim)" }}>—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {detail && <SymbolDetail q={detail} onClose={() => setDetail(null)} />}
      {managing && (
        <WatchlistDetail
          quotes={quotes}
          onPick={(q) => setDetail(q)}
          onClose={() => setManaging(false)}
        />
      )}
    </div>
  );
}
