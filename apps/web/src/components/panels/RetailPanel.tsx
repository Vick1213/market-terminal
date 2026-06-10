"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  RetailLeader,
  RetailResponse,
  RetailWsMessage,
} from "@market/shared";
import { fetchRetail, fetchRetailSymbol } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";
import { MarketChart } from "../charts/MarketChart";

function sentColor(s: number | null | undefined): string {
  if (s === null || s === undefined) return "var(--text-dim)";
  if (s > 0.1) return "var(--green)";
  if (s < -0.1) return "var(--red)";
  return "var(--yellow)";
}

function zColor(z: number): string {
  if (z >= 1.5) return "var(--green)";
  if (z <= -1.5) return "var(--red)";
  return "var(--text-dim)";
}

/** Same −100..+100 bar as the macro composite, fed by retail bull/bear. */
function Gauge({ score }: { score: number }) {
  const pct = Math.min(100, Math.max(-100, score));
  const half = Math.abs(pct) / 2;
  return (
    <div className="gauge-track" title={`Retail bull/bear ${score > 0 ? "+" : ""}${score}`}>
      <div
        className="gauge-fill"
        style={{
          left: pct >= 0 ? "50%" : `${50 - half}%`,
          width: `${half}%`,
          background: pct >= 0 ? "var(--green)" : "var(--red)",
        }}
      />
      <div className="gauge-zero" />
    </div>
  );
}

function LeaderRow({
  r,
  onPick,
  compact,
}: {
  r: RetailLeader;
  onPick?: (symbol: string) => void;
  compact?: boolean;
}) {
  const delta = r.mentions_24h_ago ? r.mentions - r.mentions_24h_ago : null;
  return (
    <tr
      className="wl-row"
      onClick={onPick ? () => onPick(r.symbol) : undefined}
      style={onPick ? { cursor: "pointer" } : undefined}
      title={
        `${r.symbol} · ${r.mentions} mentions (z=${r.mention_z > 0 ? "+" : ""}${r.mention_z})` +
        (r.rank_velocity !== null ? ` · rank ${r.rank_velocity >= 0 ? "+" : ""}${r.rank_velocity}/24h` : "") +
        (r.sentiment !== null ? ` · sentiment ${r.sentiment}` : " · unscored") +
        (r.divergence ? " · DIVERGENCE: volume spiking, text bearish" : "")
      }
    >
      <td>
        <span className="wl-sym">{r.symbol}</span>
        {r.divergence && (
          <span style={{ color: "var(--red)", marginLeft: 4 }} title="volume spiking, text bearish">
            ⚠
          </span>
        )}
      </td>
      <td className="num">{r.mentions.toLocaleString()}</td>
      <td className="num" style={{ color: delta === null ? undefined : delta >= 0 ? "var(--green)" : "var(--red)" }}>
        {delta === null ? "—" : `${delta >= 0 ? "+" : ""}${delta.toLocaleString()}`}
      </td>
      <td className="num" style={{ color: zColor(r.mention_z) }}>
        {r.mention_z > 0 ? "+" : ""}
        {r.mention_z.toFixed(1)}
      </td>
      {!compact && (
        <td className="num">
          {r.rank_velocity === null ? "—" : `${r.rank_velocity >= 0 ? "+" : ""}${r.rank_velocity}`}
        </td>
      )}
      <td className="num" style={{ color: sentColor(r.sentiment) }}>
        {r.sentiment === null ? "·" : r.sentiment.toFixed(2)}
      </td>
      {!compact && (
        <td className="num" title={`sources: apewisdom${r.sentiment_sources.length ? ", " + r.sentiment_sources.join(", ") : ""}`}>
          {"●".repeat(r.sources)}
          {"○".repeat(Math.max(0, 4 - r.sources))}
        </td>
      )}
    </tr>
  );
}

/** Per-symbol drill: mention spike chart vs price + scored social tape. */
function RetailDrill({ symbol, onBack }: { symbol: string; onBack: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ["retail", symbol],
    queryFn: () => fetchRetailSymbol(symbol),
    refetchInterval: 5 * 60_000,
  });

  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        <button className="expand-btn" onClick={onBack} title="Back to leaderboard">
          ← leaderboard
        </button>
        <span className="wl-sym" style={{ marginLeft: 10, fontSize: 14 }}>
          {symbol}
        </span>
      </div>

      <div className="chart-wrap">
        <MarketChart
          chartKey={`retail:${symbol}`}
          initialSeriesIds={[`RETAIL:${symbol}`, `PRICE:${symbol}`]}
          days={14}
          height={300}
        />
      </div>

      {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}
      {data && (
        <div className="macro-detail-cols">
          <div>
            <div className="macro-detail-title">Latest social messages</div>
            {data.messages.length === 0 && (
              <div style={{ color: "var(--text-dim)", fontSize: 11 }}>
                no scored messages yet — social poll covers spiking tickers only
              </div>
            )}
            <div className="news-list">
              {data.messages.map((m, i) => (
                <div className="news-item" key={i}>
                  <div className="news-title">
                    {m.url?.startsWith("http") ? (
                      <a href={m.url} target="_blank" rel="noreferrer">
                        {m.text}
                      </a>
                    ) : (
                      m.text
                    )}
                  </div>
                  <div className="news-meta">
                    <span>{m.source}</span>
                    {m.tag && (
                      <span style={{ color: m.tag === "Bullish" ? "var(--green)" : "var(--red)" }}>
                        {m.tag}
                      </span>
                    )}
                    {m.score !== null && (
                      <span className="news-score" style={{ color: sentColor(m.score) }}>
                        {m.score > 0 ? "+" : ""}
                        {m.score.toFixed(2)}
                      </span>
                    )}
                    <span>{new Date(m.ts + "Z").toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div>
            <div className="macro-detail-title">Per-source reads</div>
            <div className="kv">
              {data.sources.map((s) => (
                <span key={s.source} style={{ display: "contents" }}>
                  <span className="k">
                    {s.source}
                    <span className="conf-note"> {s.ts.slice(5, 16).replace("T", " ")}</span>
                  </span>
                  <span className="v" style={{ color: sentColor(s.sentiment) }}>
                    {s.mentions !== null ? `${s.mentions.toLocaleString()} msgs` : "—"}
                    {s.sentiment !== null ? ` · ${s.sentiment > 0 ? "+" : ""}${s.sentiment.toFixed(2)}` : ""}
                  </span>
                </span>
              ))}
            </div>
            <div className="macro-detail-title" style={{ marginTop: 14 }}>
              Related headlines
            </div>
            {data.headlines.length === 0 && (
              <div style={{ color: "var(--text-dim)", fontSize: 11 }}>none scored yet</div>
            )}
            <div className="news-list">
              {data.headlines.map((h, i) => (
                <div className="news-item" key={i}>
                  <div className="news-title">
                    {h.url ? (
                      <a href={h.url} target="_blank" rel="noreferrer">
                        {h.title}
                      </a>
                    ) : (
                      h.title
                    )}
                  </div>
                  <div className="news-meta">
                    {h.score !== null && (
                      <span className="news-score" style={{ color: sentColor(h.score) }}>
                        {h.score > 0 ? "+" : ""}
                        {h.score.toFixed(2)}
                      </span>
                    )}
                    <span>{h.published.slice(0, 10)}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function RetailDetail({ data, onClose }: { data: RetailResponse; onClose: () => void }) {
  const [drill, setDrill] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const g = data.gauge;
  const modal = (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>
            Retail Market Score — {drill ? drill : "leaderboard"}
            {g.score !== null && (
              <span style={{ color: g.score >= 0 ? "var(--green)" : "var(--red)", marginLeft: 10 }}>
                {g.score > 0 ? "+" : ""}
                {g.score}
              </span>
            )}
          </span>
          <button className="expand-btn" title="Close (Esc)" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          {drill ? (
            <RetailDrill symbol={drill} onBack={() => setDrill(null)} />
          ) : (
            <>
              <div className="stat-row">
                <div className="stat">
                  <div
                    className="stat-value"
                    style={{ color: sentColor(g.sentiment) }}
                  >
                    {g.score === null ? "—" : `${g.score > 0 ? "+" : ""}${g.score}`}
                  </div>
                  <div className="stat-label">bull/bear (−100..+100)</div>
                </div>
                <div className="stat">
                  <div className="stat-value" style={{ color: g.chatter_z !== null ? zColor(g.chatter_z) : undefined }}>
                    {g.chatter_z === null ? "—" : `${g.chatter_z > 0 ? "+" : ""}${g.chatter_z}`}
                  </div>
                  <div className="stat-label">chatter z</div>
                </div>
                <div className="stat">
                  <div className="stat-value">{g.total_mentions?.toLocaleString() ?? "—"}</div>
                  <div className="stat-label">mentions / 24h</div>
                </div>
                <div className="stat">
                  <div className="stat-value">{g.scored_symbols}</div>
                  <div className="stat-label">symbols with scored text</div>
                </div>
              </div>

              <div className="chip-row" style={{ margin: "6px 0 10px" }}>
                {Object.entries(data.freshness).map(([src, ts]) => (
                  <span
                    key={src}
                    className="filing-chip"
                    style={{ color: ts ? "var(--green)" : "var(--text-dim)" }}
                    title={ts ? `last data ${ts}` : "no data yet"}
                  >
                    {ts ? "●" : "○"} {src}
                  </span>
                ))}
              </div>

              <table className="wl-table wl-table-full">
                <thead>
                  <tr>
                    <th>symbol</th>
                    <th className="num">mentions</th>
                    <th className="num">Δ24h</th>
                    <th className="num">z</th>
                    <th className="num">rank Δ</th>
                    <th className="num">sent</th>
                    <th className="num">conf</th>
                  </tr>
                </thead>
                <tbody>
                  {data.leaderboard.map((r) => (
                    <LeaderRow key={r.symbol} r={r} onPick={setDrill} />
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      </div>
    </div>
  );
  return createPortal(modal, document.body);
}

export function RetailPanel() {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["retail"],
    queryFn: () => fetchRetail(30),
    refetchInterval: 5 * 60_000, // WS pushes trigger earlier refreshes
  });

  const { status, last } = useWebSocket("retail", 10);
  // A frame arrives once per ingest run (a few per hour) and the social runs
  // change sentiment without touching computed_at — refetch on every frame.
  useEffect(() => {
    if ((last as RetailWsMessage | undefined)?.type === "retail") {
      queryClient.invalidateQueries({ queryKey: ["retail"] });
    }
  }, [last, queryClient]);

  const warming = !data || data.leaderboard.length === 0;

  const downAt = useRef<{ x: number; y: number } | null>(null);
  const onPanelMouseDown = (e: React.MouseEvent) => {
    downAt.current = { x: e.clientX, y: e.clientY };
  };
  const onPanelClick = (e: React.MouseEvent) => {
    const d = downAt.current;
    const dragged = d && Math.hypot(e.clientX - d.x, e.clientY - d.y) > 5;
    downAt.current = null;
    if (dragged || warming) return;
    if ((e.target as HTMLElement).closest("a, button, select")) return;
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
        <span>Retail Market Score</span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className={`badge ${status === "open" ? "live" : "closed"}`}>
            {status === "open" ? "WS live" : status}
          </span>
          <button
            className="expand-btn"
            title="Expand: full leaderboard + per-symbol drill-down"
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
        {isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}
        {!isLoading && warming && !isError && (
          <div style={{ color: "var(--text-dim)" }}>
            warming up — first ApeWisdom snapshot lands ~1 min after boot
          </div>
        )}

        {data && !warming && (
          <>
            <div className="regime-banner" style={{ borderColor: sentColor(data.gauge.sentiment) }}>
              <span className="regime-tag" style={{ color: sentColor(data.gauge.sentiment) }}>
                {data.gauge.score === null
                  ? "VOLUME ONLY"
                  : data.gauge.score > 10
                    ? "RETAIL BULLISH"
                    : data.gauge.score < -10
                      ? "RETAIL BEARISH"
                      : "RETAIL MIXED"}
              </span>
              <span className="regime-score" style={{ color: "var(--text-dim)", fontSize: 11 }}>
                {data.gauge.total_mentions?.toLocaleString() ?? "—"} mentions
                {data.gauge.chatter_z !== null &&
                  ` · chatter z ${data.gauge.chatter_z > 0 ? "+" : ""}${data.gauge.chatter_z}`}
              </span>
            </div>
            {data.gauge.score !== null && <Gauge score={data.gauge.score} />}

            <table className="wl-table" style={{ marginTop: 8 }}>
              <thead>
                <tr>
                  <th>spiking</th>
                  <th className="num">mentions</th>
                  <th className="num">Δ24h</th>
                  <th className="num">z</th>
                  <th className="num">sent</th>
                </tr>
              </thead>
              <tbody>
                {data.leaderboard.slice(0, 8).map((r) => (
                  <LeaderRow key={r.symbol} r={r} compact />
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
      {expanded && data && !warming && (
        <RetailDetail data={data} onClose={() => setExpanded(false)} />
      )}
    </div>
  );
}
