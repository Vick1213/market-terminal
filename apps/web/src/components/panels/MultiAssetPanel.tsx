"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import type {
  CryptoQuote,
  DepthMessage,
  DepthSnapshot,
  EquityFlow,
  MetalRow,
  QuoteMessage,
  TradeMessage,
  TradePrint,
} from "@market/shared";
import { fetchMultiAsset } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";

function fmtPrice(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return v.toFixed(2);
}

function fmtNotional(v: number): string {
  if (v >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  return `$${(v / 1e3).toFixed(0)}K`;
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtZ(v: number | null): string {
  if (v === null) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}σ`;
}

function sideColor(side: string | null | undefined): string {
  if (side === "buy") return "var(--green)";
  if (side === "sell") return "var(--red)";
  return "var(--text-dim)";
}

function zColor(v: number | null): string {
  if (v === null) return "var(--text-dim)";
  if (v > 0.5) return "var(--green)";
  if (v < -0.5) return "var(--red)";
  return "var(--text)";
}

/** Dedupe key for merging WS prints with the persisted tape. */
function printKey(p: TradePrint): string {
  return `${p.exchange}|${p.symbol}|${p.ts}|${p.notional.toFixed(0)}`;
}

// --- per-panel view filters, persisted across sessions -----------------------

interface MaFilters {
  symbol: string; // "ALL" or an exact stream symbol
  min: number | null; // notional floor ($)
  max: number | null; // notional cap ($)
}

const FILTERS_KEY = "ma-filters";
const DEFAULT_FILTERS: MaFilters = { symbol: "ALL", min: null, max: null };
const MIN_PRESETS: { label: string; value: number | null }[] = [
  { label: "any $", value: null },
  { label: "≥ $50K", value: 50_000 },
  { label: "≥ $100K", value: 100_000 },
  { label: "≥ $250K", value: 250_000 },
  { label: "≥ $500K", value: 500_000 },
  { label: "≥ $1M", value: 1_000_000 },
];

function loadFilters(): MaFilters {
  if (typeof window === "undefined") return DEFAULT_FILTERS;
  try {
    const raw = window.localStorage.getItem(FILTERS_KEY);
    return raw ? { ...DEFAULT_FILTERS, ...JSON.parse(raw) } : DEFAULT_FILTERS;
  } catch {
    return DEFAULT_FILTERS;
  }
}

function matchesFilters(f: MaFilters, symbol: string, notional?: number): boolean {
  if (f.symbol !== "ALL" && symbol !== f.symbol) return false;
  if (notional !== undefined) {
    if (f.min !== null && notional < f.min) return false;
    if (f.max !== null && notional > f.max) return false;
  }
  return true;
}

/** Symbol chips + min-notional presets (the compact filter bar). */
function FilterBar({
  symbols,
  filters,
  onChange,
}: {
  symbols: string[];
  filters: MaFilters;
  onChange: (f: MaFilters) => void;
}) {
  return (
    <div className="ma-filters">
      {["ALL", ...symbols].map((s) => (
        <button
          key={s}
          className={`ma-chip ${filters.symbol === s ? "active" : ""}`}
          title={s === "ALL" ? "Show every stream" : `Only ${s}`}
          onClick={(e) => {
            e.stopPropagation();
            onChange({ ...filters, symbol: s });
          }}
        >
          {s === "ALL" ? "all" : s.split("/")[0]}
        </button>
      ))}
      <select
        className="ma-select"
        value={String(filters.min ?? "")}
        title="Minimum print notional"
        onChange={(e) =>
          onChange({ ...filters, min: e.target.value === "" ? null : Number(e.target.value) })
        }
      >
        {MIN_PRESETS.map((p) => (
          <option key={p.label} value={String(p.value ?? "")}>
            {p.label}
          </option>
        ))}
      </select>
      {(filters.max !== null || filters.symbol !== "ALL" || filters.min !== null) && (
        <button
          className="ma-chip"
          title="Reset filters"
          onClick={(e) => {
            e.stopPropagation();
            onChange(DEFAULT_FILTERS);
          }}
        >
          ✕
        </button>
      )}
    </div>
  );
}

function TapeRow({ p }: { p: TradePrint }) {
  return (
    <li className="tape-row">
      <span className="tape-time">{fmtTime(p.ts)}</span>
      <span className="tape-sym">{p.symbol}</span>
      <span className="tape-side" style={{ color: sideColor(p.side) }}>
        {p.side ?? "?"}
      </span>
      <span className="tape-notional" style={{ color: sideColor(p.side) }}>
        {fmtNotional(p.notional)}
      </span>
      <span className="tape-px">@ {fmtPrice(p.price)}</span>
      <span className="tape-ex">{p.exchange}</span>
    </li>
  );
}

/** Bid/ask depth imbalance bar, -1 (ask-heavy) .. +1 (bid-heavy). */
function ImbalanceBar({ b }: { b: DepthSnapshot }) {
  const pct = (b.imbalance + 1) / 2; // 0..1
  return (
    <div className="imb-row" title={`bid ${b.bid_depth.toFixed(2)} / ask ${b.ask_depth.toFixed(2)} (top 10)`}>
      <span className="imb-label">
        {b.symbol} <span className="tape-ex">{b.exchange}</span>
      </span>
      <div className="imb-track">
        <div className="imb-fill" style={{ width: `${(pct * 100).toFixed(1)}%` }} />
        <div className="imb-mid" />
      </div>
      <span className="imb-val" style={{ color: zColor(b.imbalance) }}>
        {(b.imbalance * 100).toFixed(0)}%
      </span>
    </div>
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

/** Expanded view: full tape + depth + metals basis + equity flow components. */
function MultiAssetDetail({
  live,
  quotes,
  books,
  tape,
  metals,
  equities,
  dominance,
  symbols,
  filters,
  onFilters,
  onClose,
}: {
  live: boolean;
  quotes: CryptoQuote[];
  books: DepthSnapshot[];
  tape: TradePrint[];
  metals: MetalRow[];
  equities: EquityFlow[];
  dominance: number | null;
  symbols: string[];
  filters: MaFilters;
  onFilters: (f: MaFilters) => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const bySymbol = useMemo(() => {
    const m = new Map<string, CryptoQuote>();
    for (const q of quotes) {
      const prev = m.get(q.symbol);
      if (q.price !== null && (!prev || (q.ts ?? "") > (prev.ts ?? ""))) m.set(q.symbol, q);
    }
    return m;
  }, [quotes]);

  const modal = (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>
            Multi-Asset Liquidity &amp; Major Trades
            <span className={`badge ${live ? "live" : "closed"}`} style={{ marginLeft: 10 }}>
              {live ? "LIVE" : "OFFLINE"}
            </span>
          </span>
          <button className="expand-btn" title="Close (Esc)" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          <div className="stat-row">
            {[...bySymbol.values()].map((q) => (
              <Stat key={q.symbol} label={`${q.symbol} (live)`} value={fmtPrice(q.price)} />
            ))}
            {metals.map((m) => (
              <Stat key={m.symbol} label={`${m.symbol} spot`} value={fmtPrice(m.spot)} />
            ))}
            {dominance !== null && <Stat label="BTC dominance" value={`${dominance.toFixed(1)}%`} />}
          </div>

          <div className="ma-filters-row">
            <FilterBar symbols={symbols} filters={filters} onChange={onFilters} />
            <span className="ma-range">
              $ range
              <input
                className="ma-range-input"
                type="number"
                min={0}
                placeholder="min"
                value={filters.min ?? ""}
                onChange={(e) =>
                  onFilters({
                    ...filters,
                    min: e.target.value === "" ? null : Math.max(0, Number(e.target.value)),
                  })
                }
              />
              –
              <input
                className="ma-range-input"
                type="number"
                min={0}
                placeholder="max"
                value={filters.max ?? ""}
                onChange={(e) =>
                  onFilters({
                    ...filters,
                    max: e.target.value === "" ? null : Math.max(0, Number(e.target.value)),
                  })
                }
              />
            </span>
          </div>

          <div className="macro-detail-title">Order-book imbalance (top 10 levels)</div>
          {books.length === 0 && <div className="ma-empty">no depth snapshots for this filter</div>}
          {books.map((b) => (
            <ImbalanceBar key={`${b.exchange}|${b.symbol}`} b={b} />
          ))}

          <div className="macro-detail-title">
            Large prints <span className="conf-note">stream captures ≥ $250K notional or ≥ 4σ trade size</span>
          </div>
          {tape.length === 0 && <div className="ma-empty">no prints match the current filter…</div>}
          <ul className="tape tape-full">
            {tape.slice(0, 40).map((p) => (
              <TapeRow key={printKey(p)} p={p} />
            ))}
          </ul>

          <div className="macro-detail-title">
            Metals <span className="conf-note">spot ~60s · futures + ETF flow are EOD proxies</span>
          </div>
          <table className="wl-table wl-table-full">
            <thead>
              <tr>
                <th>metal</th>
                <th className="num">spot</th>
                <th className="num">futures (EOD)</th>
                <th className="num">basis</th>
                <th className="num">basis%</th>
                <th>ETF</th>
                <th className="num">ETF vol z</th>
              </tr>
            </thead>
            <tbody>
              {metals.map((m) => (
                <tr key={m.symbol}>
                  <td>
                    <span className="wl-sym">{m.symbol}</span>
                  </td>
                  <td className="num">{fmtPrice(m.spot)}</td>
                  <td className="num">{fmtPrice(m.futures_close)}</td>
                  <td className="num">{m.basis !== null ? m.basis.toFixed(1) : "—"}</td>
                  <td className="num">{m.basis_pct !== null ? `${m.basis_pct.toFixed(2)}%` : "—"}</td>
                  <td>{m.etf_symbol ?? "—"}</td>
                  <td className="num" style={{ color: zColor(m.etf_volume_z) }}>
                    {fmtZ(m.etf_volume_z)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="macro-detail-title">
            Equity accumulation score{" "}
            <span className="conf-note">
              blend of unusual-volume z + FINRA short-vol trend (T+1, not short interest)
            </span>
          </div>
          <table className="wl-table wl-table-full">
            <thead>
              <tr>
                <th>symbol</th>
                <th className="num">volume z</th>
                <th className="num">short ratio</th>
                <th className="num">short trend</th>
                <th className="num">accumulation</th>
                <th>as of</th>
              </tr>
            </thead>
            <tbody>
              {equities.map((e) => (
                <tr key={e.symbol}>
                  <td>
                    <span className="wl-sym">{e.symbol}</span>
                  </td>
                  <td className="num" style={{ color: zColor(e.volume_z) }}>
                    {fmtZ(e.volume_z)}
                  </td>
                  <td className="num">
                    {e.short_ratio !== null ? `${(e.short_ratio * 100).toFixed(1)}%` : "—"}
                  </td>
                  <td className="num" style={{ color: zColor(e.short_trend_z !== null ? -e.short_trend_z : null) }}>
                    {fmtZ(e.short_trend_z)}
                  </td>
                  <td className="num" style={{ color: zColor(e.accumulation) }}>
                    {e.accumulation !== null ? e.accumulation.toFixed(2) : "—"}
                  </td>
                  <td style={{ color: "var(--text-dim)" }}>{e.ts ? e.ts.slice(0, 10) : "—"}</td>
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

export function MultiAssetPanel() {
  const [expanded, setExpanded] = useState(false);
  const [filters, setFilters] = useState<MaFilters>(loadFilters);

  const onFilters = (f: MaFilters) => {
    setFilters(f);
    try {
      window.localStorage.setItem(FILTERS_KEY, JSON.stringify(f));
    } catch {
      /* private mode etc. — filters just won't persist */
    }
  };

  const { data } = useQuery({
    queryKey: ["multiasset"],
    queryFn: fetchMultiAsset,
    refetchInterval: 60_000, // metals spot cadence; crypto rides the WS
  });

  const { status, messages } = useWebSocket("trades", 300);

  // Newest-first WS prints merged over the persisted tape (dedupe on key).
  const allTape = useMemo(() => {
    const ws = messages.filter((m) => m.type === "trade") as TradeMessage[];
    const seen = new Set<string>();
    const out: TradePrint[] = [];
    for (const p of [...ws, ...(data?.crypto.prints ?? [])]) {
      const k = printKey(p);
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(p);
    }
    return out;
  }, [messages, data]);

  // Live quotes: latest WS quote frame beats the REST snapshot.
  const quotes = useMemo(() => {
    const frame = messages.find((m) => m.type === "quote") as QuoteMessage | undefined;
    return frame?.quotes ?? data?.crypto.quotes ?? [];
  }, [messages, data]);

  // Latest depth per stream (WS over REST).
  const allBooks = useMemo(() => {
    const byKey = new Map<string, DepthSnapshot>();
    for (const b of data?.crypto.books ?? []) byKey.set(`${b.exchange}|${b.symbol}`, b);
    const depths = messages.filter((m) => m.type === "depth") as DepthMessage[];
    for (let i = depths.length - 1; i >= 0; i--) {
      const d = depths[i];
      byKey.set(`${d.exchange}|${d.symbol}`, d);
    }
    return [...byKey.values()];
  }, [messages, data]);

  // Streamed symbols drive the filter chips (stable even when the tape is quiet).
  const symbols = useMemo(
    () => [...new Set([...quotes.map((q) => q.symbol), ...allTape.map((p) => p.symbol)])].sort(),
    [quotes, allTape],
  );

  const tape = useMemo(
    () => allTape.filter((p) => matchesFilters(filters, p.symbol, p.notional)),
    [allTape, filters],
  );
  const books = useMemo(
    () => allBooks.filter((b) => matchesFilters(filters, b.symbol)),
    [allBooks, filters],
  );
  const shownQuotes = useMemo(() => {
    const m = new Map<string, CryptoQuote>();
    for (const q of quotes) {
      if (!matchesFilters(filters, q.symbol)) continue;
      const prev = m.get(q.symbol);
      if (q.price !== null && (!prev || (q.ts ?? "") > (prev.ts ?? ""))) m.set(q.symbol, q);
    }
    return [...m.values()];
  }, [quotes, filters]);

  const live = status === "open" && (quotes.some((q) => q.connected) || (data?.crypto.live ?? false));
  const metals = data?.metals ?? [];
  const equities = (data?.equities ?? []).filter((e) => e.accumulation !== null);

  // Expand on a TRUE click only — same drag-safe pattern as the other panels.
  const downAt = useRef<{ x: number; y: number } | null>(null);
  const onPanelMouseDown = (e: React.MouseEvent) => {
    downAt.current = { x: e.clientX, y: e.clientY };
  };
  const onPanelClick = (e: React.MouseEvent) => {
    const d = downAt.current;
    const dragged = d && Math.hypot(e.clientX - d.x, e.clientY - d.y) > 5;
    downAt.current = null;
    if (dragged) return;
    if ((e.target as HTMLElement).closest("a, button, select, input")) return;
    setExpanded(true);
  };

  return (
    <div
      className="panel panel-expandable"
      onMouseDownCapture={onPanelMouseDown}
      onClick={onPanelClick}
      title="Click to expand"
    >
      <div className="panel-head">
        <span>Multi-Asset Liquidity &amp; Major Trades</span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            className={`badge ${live ? "live" : "closed"}`}
            title={live ? "Crypto prints are real-time public WS" : "Streams connecting…"}
          >
            {live ? "LIVE" : "…"}
          </span>
          <button
            className="expand-btn"
            title="Expand: full tape + depth + metals + equity flow"
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(true);
            }}
          >
            ⤢
          </button>
        </span>
      </div>

      <div className="panel-body ma-body">
        <div className="ma-quotes">
          {shownQuotes.map((q) => (
            <span key={q.symbol} className="ma-quote">
              <span className="wl-sym">{q.symbol}</span> {fmtPrice(q.price)}
            </span>
          ))}
          {filters.symbol === "ALL" &&
            metals.map((m) => (
              <span key={m.symbol} className="ma-quote ma-quote-proxy" title="spot ~60s delayed">
                <span className="wl-sym">{m.symbol}</span> {fmtPrice(m.spot)}
              </span>
            ))}
        </div>

        <FilterBar symbols={symbols} filters={filters} onChange={onFilters} />

        <ul className="tape ma-tape-scroll">
          {tape.length === 0 && (
            <li className="ma-empty">
              {allTape.length === 0
                ? "waiting for large prints (≥ $250K or ≥ 4σ) — BTC/ETH on Coinbase + Kraken"
                : "no prints match the current filter"}
            </li>
          )}
          {tape.slice(0, 30).map((p) => (
            <TapeRow key={printKey(p)} p={p} />
          ))}
        </ul>

        {equities.length > 0 && (
          <div className="ma-flow-strip" title="Accumulation: unusual-volume z blended with FINRA short-vol trend (EOD/T+1)">
            {equities.map((e) => (
              <span key={e.symbol} className="ma-flow-chip" style={{ color: zColor(e.accumulation) }}>
                {e.symbol} {e.accumulation! > 0 ? "+" : ""}
                {e.accumulation!.toFixed(1)}
              </span>
            ))}
          </div>
        )}
      </div>

      {expanded && (
        <MultiAssetDetail
          live={live}
          quotes={quotes.filter((q) => matchesFilters(filters, q.symbol))}
          books={books}
          tape={tape}
          metals={metals}
          equities={data?.equities ?? []}
          dominance={data?.crypto.btc_dominance ?? null}
          symbols={symbols}
          filters={filters}
          onFilters={onFilters}
          onClose={() => setExpanded(false)}
        />
      )}
    </div>
  );
}
