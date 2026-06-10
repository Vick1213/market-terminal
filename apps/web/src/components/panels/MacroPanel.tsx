"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { MacroBucket, MacroMessage, MacroResponse } from "@market/shared";
import { fetchMacro } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";
import { MarketChart } from "../charts/MarketChart";

const REGIME_COLOR: Record<string, string> = {
  "risk-on": "var(--green)",
  neutral: "var(--yellow)",
  "risk-off": "var(--red)",
  stress: "var(--red)",
};

function regimeColor(regime: string): string {
  return REGIME_COLOR[regime] ?? "var(--text-dim)";
}

/** Composite gauge: −100..+100 horizontal bar with a centered zero line. */
function Gauge({ score }: { score: number }) {
  const pct = Math.min(100, Math.max(-100, score));
  const half = Math.abs(pct) / 2; // % of track width
  return (
    <div className="gauge-track" title={`Composite ${score > 0 ? "+" : ""}${score}`}>
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

/** One sub-bucket: label, signed contribution bar, z value. */
function BucketRow({ b }: { b: MacroBucket }) {
  if (b.z === null) {
    return (
      <div className="bucket-row dim" title="No data ingested yet for this bucket">
        <span className="bucket-label">{b.label}</span>
        <div className="bucket-track" />
        <span className="bucket-val">—</span>
      </div>
    );
  }
  const half = Math.min(100, Math.abs(b.contribution)) / 2;
  const tip = b.components
    .map((c) => `${c.series_id} z=${c.z > 0 ? "+" : ""}${c.z.toFixed(2)} (${c.value})`)
    .join("\n");
  return (
    <div className="bucket-row" title={tip}>
      <span className="bucket-label">
        {b.label} <span className="bucket-weight">{Math.round(b.weight_used * 100)}%</span>
      </span>
      <div className="bucket-track">
        <div
          className="bucket-fill"
          style={{
            left: b.contribution >= 0 ? "50%" : `${50 - half}%`,
            width: `${half}%`,
            background: b.contribution >= 0 ? "var(--green)" : "var(--red)",
          }}
        />
      </div>
      <span
        className="bucket-val"
        style={{ color: b.contribution >= 0 ? "var(--green)" : "var(--red)" }}
      >
        {b.contribution > 0 ? "+" : ""}
        {b.contribution.toFixed(1)}
      </span>
    </div>
  );
}

/** Composite-score history sparkline (same minimal SVG style as NewsPanel). */
function HistorySpark({ data }: { data: MacroResponse }) {
  const points = data.history;
  if (points.length < 2) return null;
  const w = 120;
  const h = 24;
  const path = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h / 2 - (p.score / 100) * (h / 2 - 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const last = points[points.length - 1].score;
  return (
    <svg width={w} height={h} className="sparkline" aria-label="composite history">
      <line x1={0} y1={h / 2} x2={w} y2={h / 2} stroke="var(--panel-border)" strokeWidth={1} />
      <polyline
        points={path}
        fill="none"
        stroke={last >= 0 ? "var(--green)" : "var(--red)"}
        strokeWidth={1.5}
      />
    </svg>
  );
}

const DIAL_FORMAT: Record<string, (v: number) => string> = {
  FINRA_SHORT_RATIO: (v) => `${(v * 100).toFixed(1)}%`,
  NAAIM_EXPOSURE: (v) => v.toFixed(1),
};

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

/** Expanded view: composite chart (overlay anything) + the full breakdown —
 * sub-bucket contributions with per-component z-scores, regime dials, and
 * the headline readings with their as-of dates. */
function MacroDetail({ data, onClose }: { data: MacroResponse; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const live = data.buckets.filter((b) => b.z !== null);
  const dials = Object.entries(data.dials);

  const modal = (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>
            Liquidity &amp; Macro — detail
            <span style={{ color: regimeColor(data.regime), marginLeft: 10 }}>
              {data.regime.toUpperCase()}
            </span>
          </span>
          <button className="expand-btn" title="Close (Esc)" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          <div className="stat-row">
            <Stat
              label="composite (−100..+100)"
              value={`${(data.score ?? 0) > 0 ? "+" : ""}${data.score}`}
              color={(data.score ?? 0) >= 0 ? "var(--green)" : "var(--red)"}
            />
            <Stat label="regime" value={data.regime} color={regimeColor(data.regime)} />
            <Stat label="buckets live" value={`${live.length} / ${data.buckets.length}`} />
            <Stat
              label="computed"
              value={
                data.computed_at
                  ? new Date(data.computed_at).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  : "—"
              }
            />
          </div>

          <div className="chart-wrap">
            <MarketChart
              chartKey="macro"
              initialSeriesIds={["COMPOSITE_RISK", "VIX"]}
              days={730}
              height={380}
            />
          </div>

          <div className="macro-detail-cols">
            <div>
              <div className="macro-detail-title">Sub-bucket contributions</div>
              <div className="bucket-list">
                {data.buckets.map((b) => (
                  <BucketRow key={b.key} b={b} />
                ))}
              </div>
              {data.buckets.map(
                (b) =>
                  b.components.length > 0 && (
                    <div key={b.key} className="macro-components">
                      <span className="k">{b.label}:</span>{" "}
                      {b.components
                        .map((c) => `${c.series_id} z=${c.z > 0 ? "+" : ""}${c.z.toFixed(2)} (${c.value}, ${c.ts})`)
                        .join(" · ")}
                    </div>
                  )
              )}
            </div>
            <div>
              <div className="macro-detail-title">Regime dials</div>
              {dials.length === 0 && (
                <div style={{ color: "var(--text-dim)", fontSize: 11 }}>
                  no dials yet — needs FRED history
                </div>
              )}
              <div className="kv">
                {dials.map(([key, d]) => (
                  <span key={key} style={{ display: "contents" }}>
                    <span className="k">
                      {d.vote > 0 ? "▲" : "▼"} {d.label}
                    </span>
                    <span className="v" style={{ color: d.vote > 0 ? "var(--green)" : "var(--red)" }}>
                      {d.value > 0 ? "+" : ""}
                      {d.value}
                    </span>
                  </span>
                ))}
              </div>
              <div className="macro-detail-title" style={{ marginTop: 14 }}>
                Headline readings
              </div>
              <div className="kv">
                {data.headline.map((h) => (
                  <span key={h.series_id} style={{ display: "contents" }}>
                    <span className="k" title={h.series_id}>
                      {h.label}
                      <span className="conf-note"> {h.ts.slice(0, 10)}</span>
                    </span>
                    <span className="v">
                      {(DIAL_FORMAT[h.series_id] ?? ((v: number) => v.toFixed(2)))(h.value)}
                    </span>
                  </span>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
  return createPortal(modal, document.body);
}

export function MacroPanel() {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["macro"],
    queryFn: fetchMacro,
    refetchInterval: 5 * 60_000, // daily-cadence data; WS pushes trigger earlier
  });

  const { status, last } = useWebSocket("macro", 10);

  // A WS frame only carries score/regime — refetch the full snapshot.
  const lastComputed = (last as MacroMessage | undefined)?.computed_at;
  useEffect(() => {
    if (lastComputed && lastComputed !== data?.computed_at) {
      queryClient.invalidateQueries({ queryKey: ["macro"] });
    }
  }, [lastComputed, data?.computed_at, queryClient]);

  const warming = !data || data.score === null;

  // VIX term structure stress flag is worth surfacing even when not in stress
  // regime (card #11: <1.0 = backwardation).
  const dials = useMemo(() => Object.entries(data?.dials ?? {}), [data]);

  // Expand on a TRUE click only: not on buttons/links, and not on the
  // mouse-up that ends a react-grid-layout drag (same pattern as NewsPanel).
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
        <span>Liquidity &amp; Macro</span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {data && !warming && <HistorySpark data={data} />}
          <span className={`badge ${status === "open" ? "live" : "closed"}`}>
            {status === "open" ? "WS live" : status}
          </span>
          <button
            className="expand-btn"
            title="Expand: full breakdown + chart (overlay VIX, HY OAS, prices…)"
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
            warming up — FRED/CBOE/FINRA ingest runs shortly after boot
          </div>
        )}

        {data && !warming && (
          <>
            <div className="regime-banner" style={{ borderColor: regimeColor(data.regime) }}>
              <span className="regime-tag" style={{ color: regimeColor(data.regime) }}>
                {data.regime.toUpperCase()}
              </span>
              <span
                className="regime-score"
                style={{ color: (data.score ?? 0) >= 0 ? "var(--green)" : "var(--red)" }}
              >
                {(data.score ?? 0) > 0 ? "+" : ""}
                {data.score}
              </span>
            </div>
            <Gauge score={data.score ?? 0} />

            <div className="bucket-list">
              {data.buckets.map((b) => (
                <BucketRow key={b.key} b={b} />
              ))}
            </div>

            {dials.length > 0 && (
              <div className="chip-row" style={{ marginTop: 10 }}>
                {dials.map(([key, d]) => (
                  <span
                    key={key}
                    className="filing-chip"
                    style={{ color: d.vote > 0 ? "var(--green)" : "var(--red)" }}
                    title={`${d.label}: ${d.value} (${d.vote > 0 ? "risk-on" : "risk-off"} vote)`}
                  >
                    {d.vote > 0 ? "▲" : "▼"} {d.label}
                  </span>
                ))}
              </div>
            )}

            <div className="kv" style={{ marginTop: 10 }}>
              {data.headline.map((d) => (
                <span key={d.series_id} style={{ display: "contents" }}>
                  <span className="k" title={`${d.series_id} as of ${d.ts.slice(0, 10)}`}>
                    {d.label}
                  </span>
                  <span className="v">
                    {(DIAL_FORMAT[d.series_id] ?? ((v: number) => v.toFixed(2)))(d.value)}
                  </span>
                </span>
              ))}
            </div>
          </>
        )}
      </div>
      {expanded && data && !warming && (
        <MacroDetail data={data} onClose={() => setExpanded(false)} />
      )}
    </div>
  );
}
