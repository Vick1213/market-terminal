"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { CorrCard, CorrResponse, CorrWsMessage } from "@market/shared";
import { fetchCorr, fetchCorrCard } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";

const STATUS_COLOR: Record<string, string> = {
  holds: "var(--green)",
  watch: "var(--yellow)",
  broken: "var(--red)",
  "no-data": "var(--text-dim)",
};

const REGIME_COLOR: Record<string, string> = {
  "risk-on": "var(--green)",
  neutral: "var(--yellow)",
  "risk-off": "var(--red)",
  stress: "var(--red)",
};

function statusColor(s: string): string {
  return STATUS_COLOR[s] ?? "var(--text-dim)";
}

function fmtCorr(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}`;
}

function fmtZ(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}σ`;
}

/** Generic multi-line SVG time chart: optional ±2σ baseline band + zero line.
 * Lines skip null gaps; y auto-scales unless a fixed domain is given. */
function SvgLines({
  data,
  lines,
  band,
  yDomain,
  height = 170,
}: {
  data: Record<string, number | null>[];
  lines: { key: string; color: string; label: string }[];
  band?: { mean: number; std: number } | null;
  yDomain?: [number, number];
  height?: number;
}) {
  const w = 640;
  const pad = 4;
  const pts = data as ({ t: number } & Record<string, number | null>)[];
  if (pts.length < 2) {
    return <div className="chart-empty">not enough history yet</div>;
  }
  const t0 = pts[0].t;
  const t1 = pts[pts.length - 1].t;
  let lo = Infinity;
  let hi = -Infinity;
  if (yDomain) {
    [lo, hi] = yDomain;
  } else {
    for (const p of pts) {
      for (const l of lines) {
        const v = p[l.key];
        if (v !== null && v !== undefined) {
          lo = Math.min(lo, v);
          hi = Math.max(hi, v);
        }
      }
    }
    if (band) {
      lo = Math.min(lo, band.mean - 2 * band.std);
      hi = Math.max(hi, band.mean + 2 * band.std);
    }
    if (!isFinite(lo) || !isFinite(hi)) return <div className="chart-empty">no data</div>;
    const margin = (hi - lo) * 0.06 || 0.1;
    lo -= margin;
    hi += margin;
  }
  const x = (t: number) => pad + ((t - t0) / Math.max(1, t1 - t0)) * (w - 2 * pad);
  const y = (v: number) => pad + (1 - (v - lo) / (hi - lo)) * (height - 2 * pad);

  const paths = lines.map((l) => {
    let d = "";
    let pen = false;
    for (const p of pts) {
      const v = p[l.key];
      if (v === null || v === undefined) {
        pen = false;
        continue;
      }
      d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    }
    return { ...l, d };
  });

  const fmtDate = (t: number) =>
    new Date(t * 1000).toLocaleDateString([], { month: "short", year: "2-digit" });

  return (
    <div>
      <svg
        viewBox={`0 0 ${w} ${height}`}
        style={{ width: "100%", height: "auto", display: "block" }}
      >
        {band && (
          <rect
            x={pad}
            y={y(band.mean + 2 * band.std)}
            width={w - 2 * pad}
            height={Math.abs(y(band.mean - 2 * band.std) - y(band.mean + 2 * band.std))}
            fill="rgba(120,123,134,0.10)"
          />
        )}
        {band && (
          <line
            x1={pad}
            x2={w - pad}
            y1={y(band.mean)}
            y2={y(band.mean)}
            stroke="var(--text-dim)"
            strokeWidth={1}
            strokeDasharray="4 4"
          />
        )}
        {lo < 0 && hi > 0 && (
          <line x1={pad} x2={w - pad} y1={y(0)} y2={y(0)} stroke="var(--panel-border)" strokeWidth={1} />
        )}
        {paths.map((p) => (
          <path key={p.key} d={p.d} fill="none" stroke={p.color} strokeWidth={1.5} />
        ))}
      </svg>
      <div className="chart-legend">
        {lines.map((l) => (
          <span key={l.key}>
            <span className="legend-line" style={{ background: l.color }} />
            {l.label}
          </span>
        ))}
        {band && (
          <span>
            <span className="legend-band" />
            baseline ±2σ
          </span>
        )}
        <span style={{ marginLeft: "auto", color: "var(--text-dim)" }}>
          {fmtDate(t0)} – {fmtDate(t1)}
        </span>
      </div>
    </div>
  );
}

/** Cross-correlation profile bars; the peak lag is highlighted. */
function LeadLagBars({ card }: { card: CorrCard }) {
  const ll = card.lead_lag;
  if (!ll || ll.profile.length === 0) return null;
  const maxAbs = Math.max(...ll.profile.map((p) => Math.abs(p.corr ?? 0)), 0.01);
  return (
    <div>
      <div className="macro-detail-title">
        Lead–lag scan{" "}
        <span className="conf-note">
          peak {ll.peak_lag > 0 ? "+" : ""}
          {ll.peak_lag}
          {ll.unit} (corr {fmtCorr(ll.peak_corr)}) —{" "}
          {ll.decayed ? `${ll.leader} no longer leading` : `${ll.leader} leads`}
        </span>
      </div>
      <div className="leadlag-bars">
        {ll.profile.map((p) => {
          const h = (Math.abs(p.corr ?? 0) / maxAbs) * 100;
          const isPeak = p.lag === ll.peak_lag;
          return (
            <div
              key={p.lag}
              style={{ flex: 1, height: "100%", display: "flex", alignItems: "flex-end" }}
              title={`lag ${p.lag}${ll.unit}: corr ${fmtCorr(p.corr)}`}
            >
              <div
                style={{
                  width: "100%",
                  height: `${h}%`,
                  background: isPeak
                    ? "var(--accent)"
                    : (p.corr ?? 0) >= 0
                      ? "rgba(38,166,154,0.45)"
                      : "rgba(239,83,80,0.45)",
                  borderRadius: 1,
                }}
              />
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: "var(--text-dim)" }}>
        <span>
          {ll.profile[0].lag}
          {ll.unit}
        </span>
        <span>
          lag ({ll.leader} leads →) {ll.profile[ll.profile.length - 1].lag}
          {ll.unit}
        </span>
      </div>
    </div>
  );
}

function heatColor(v: number | null): string {
  if (v === null) return "transparent";
  const a = Math.min(1, Math.abs(v)) * 0.55;
  return v >= 0 ? `rgba(38,166,154,${a})` : `rgba(239,83,80,${a})`;
}

function Heatmap({ data }: { data: CorrResponse }) {
  const { labels, matrix } = data.heatmap;
  if (labels.length === 0) return null;
  return (
    <div style={{ overflowX: "auto" }}>
      <div className="macro-detail-title">90d correlation heatmap (log-returns)</div>
      <table className="heat-table">
        <thead>
          <tr>
            <th />
            {labels.map((l) => (
              <th key={l}>{l}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {labels.map((row, i) => (
            <tr key={row}>
              <th className="row-label">{row}</th>
              {labels.map((col, j) => (
                <td
                  key={col}
                  style={{ background: i === j ? "transparent" : heatColor(matrix[i][j]) }}
                  title={`${row} vs ${col}: ${fmtCorr(matrix[i][j])}`}
                >
                  {i === j ? "·" : fmtCorr(matrix[i][j])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** One rule-card tile (compact panel + full modal share it). */
function CardTile({
  c,
  onPick,
  full,
}: {
  c: CorrCard;
  onPick: (id: string) => void;
  full?: boolean;
}) {
  const headline =
    c.mode === "corr"
      ? `${c.windows[1]} ${fmtCorr(c.corr90)}`
      : c.value_label || fmtCorr(c.value);
  return (
    <div
      className="corr-card"
      style={{ borderLeftColor: statusColor(c.status) }}
      onClick={() => c.status !== "no-data" && onPick(c.id)}
      title={`#${c.num} ${c.title} — ${c.status.toUpperCase()}\n${c.normal}\n${c.rationale}${
        c.notes.length ? "\n⚠ " + c.notes.join("\n⚠ ") : ""
      }`}
    >
      <div className="corr-card-head">
        <span className="corr-card-title">
          <span style={{ color: "var(--text-dim)" }}>{c.num}</span> {c.title}
        </span>
        <span className="corr-status" style={{ color: statusColor(c.status) }}>
          {c.status === "no-data" ? "·" : c.status.toUpperCase()}
        </span>
      </div>
      <div className="corr-card-vals">
        <span style={{ color: "var(--text)" }}>{headline}</span>
        {c.mode === "corr" && <span>{c.windows[0]} {fmtCorr(c.corr30)}</span>}
        {c.z !== null && <span>z {fmtZ(c.z)}</span>}
        {c.sign_flip && <span style={{ color: "var(--red)" }}>flip</span>}
      </div>
      {full && (c.notes.length > 0 || c.lead_lag?.decayed) && (
        <div className="corr-card-note" title={c.notes.join("\n")}>
          ⚠ {c.notes[0] ?? `${c.lead_lag?.leader} stopped leading`}
        </div>
      )}
    </div>
  );
}

/** Per-card drill: rolling-corr history vs baseline band, rebased legs,
 * lead-lag profile, and the rule's plain-English context. */
function CorrDrill({ cardId, onBack }: { cardId: string; onBack: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ["corr", cardId],
    queryFn: () => fetchCorrCard(cardId),
    refetchInterval: 15 * 60_000,
  });

  if (isLoading || !data) {
    return (
      <div>
        <button className="expand-btn" onClick={onBack}>
          ← all cards
        </button>
        <div style={{ color: "var(--text-dim)", marginTop: 10 }}>computing…</div>
      </div>
    );
  }
  const c = data.card;
  const band =
    c.baseline_mean !== null && c.baseline_std !== null
      ? { mean: c.baseline_mean, std: c.baseline_std }
      : null;

  return (
    <div>
      <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 10 }}>
        <button className="expand-btn" onClick={onBack} title="Back to all cards">
          ← all cards
        </button>
        <span className="wl-sym" style={{ fontSize: 14 }}>
          #{c.num} {c.title}
        </span>
        <span className="corr-status" style={{ color: statusColor(c.status), fontSize: 11 }}>
          {c.status.toUpperCase()}
        </span>
        {c.asof && <span className="conf-note">as of {c.asof}</span>}
      </div>

      <div className="stat-row">
        {c.mode === "corr" ? (
          <>
            <div className="stat">
              <div className="stat-value">{fmtCorr(c.corr30)}</div>
              <div className="stat-label">corr {c.windows[0]}</div>
            </div>
            <div className="stat">
              <div className="stat-value">{fmtCorr(c.corr90)}</div>
              <div className="stat-label">corr {c.windows[1]}</div>
            </div>
            <div className="stat">
              <div className="stat-value">
                {c.baseline_mean !== null ? fmtCorr(c.baseline_mean) : "—"}
                {c.baseline_std !== null && (
                  <span style={{ color: "var(--text-dim)", fontSize: 12 }}> ±{c.baseline_std.toFixed(2)}</span>
                )}
              </div>
              <div className="stat-label">long-run baseline (n={c.baseline_n})</div>
            </div>
          </>
        ) : (
          <div className="stat">
            <div className="stat-value">{c.value_label || fmtCorr(c.value)}</div>
            <div className="stat-label">{c.legs.join(" / ")}</div>
          </div>
        )}
        <div className="stat">
          <div className="stat-value" style={{ color: statusColor(c.status) }}>
            {fmtZ(c.z)}
            {c.sign_flip && <span style={{ color: "var(--red)", fontSize: 12 }}> + sign flip</span>}
          </div>
          <div className="stat-label">z vs baseline</div>
        </div>
      </div>

      {data.history && data.history.length > 1 && (
        <>
          <div className="macro-detail-title">
            Rolling correlation ({c.legs.join(" vs ")})
          </div>
          <SvgLines
            data={data.history as unknown as Record<string, number | null>[]}
            lines={[
              { key: "corr30", color: "var(--text-dim)", label: `corr ${c.windows[0]}` },
              { key: "corr90", color: "var(--accent)", label: `corr ${c.windows[1]}` },
            ]}
            band={band}
            yDomain={[-1, 1]}
          />
        </>
      )}

      {data.legs_rebased && data.legs_rebased.length > 1 && (
        <>
          <div className="macro-detail-title" style={{ marginTop: 12 }}>
            Legs, rebased to 100 (1y)
          </div>
          <SvgLines
            data={data.legs_rebased as unknown as Record<string, number | null>[]}
            lines={[
              { key: "a", color: "var(--accent)", label: data.leg_labels[0] },
              { key: "b", color: "var(--yellow)", label: data.leg_labels[1] ?? "" },
            ]}
            height={140}
          />
        </>
      )}

      {data.levels && data.levels.length > 1 && (
        <>
          <div className="macro-detail-title" style={{ marginTop: 12 }}>
            {data.leg_labels.join(" / ")} history
          </div>
          <SvgLines
            data={data.levels as unknown as Record<string, number | null>[]}
            lines={
              data.leg_labels.length > 1
                ? [
                    { key: "a", color: "var(--accent)", label: data.leg_labels[0] },
                    { key: "b", color: "var(--yellow)", label: data.leg_labels[1] },
                  ]
                : [{ key: "v", color: "var(--accent)", label: data.leg_labels[0] }]
            }
            height={140}
          />
        </>
      )}

      {c.lead_lag && (
        <div style={{ marginTop: 12 }}>
          <LeadLagBars card={c} />
        </div>
      )}

      <div className="macro-detail-cols" style={{ marginTop: 12 }}>
        <div>
          <div className="macro-detail-title">Rule</div>
          <div className="kv">
            <span className="k">normal</span>
            <span className="v">{c.normal}</span>
            <span className="k">why</span>
            <span className="v">{c.rationale}</span>
            <span className="k">breaks when</span>
            <span className="v">{c.breaks_when}</span>
            {c.secondary && (
              <>
                <span className="k">{c.secondary.label}</span>
                <span className="v">
                  30d {fmtCorr(c.secondary.corr30)} · 90d {fmtCorr(c.secondary.corr90)}
                </span>
              </>
            )}
          </div>
        </div>
        <div>
          {c.notes.length > 0 && (
            <>
              <div className="macro-detail-title">Flags</div>
              {c.notes.map((n, i) => (
                <div key={i} style={{ fontSize: 11, color: "var(--yellow)", marginBottom: 4 }}>
                  ⚠ {n}
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function countStatuses(cards: CorrCard[]) {
  return {
    broken: cards.filter((c) => c.status === "broken").length,
    watch: cards.filter((c) => c.status === "watch").length,
    holds: cards.filter((c) => c.status === "holds").length,
  };
}

export function CorrelationPanel() {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [drillTo, setDrillTo] = useState<string | null>(null);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["corr"],
    queryFn: fetchCorr,
    refetchInterval: 5 * 60_000, // recompute is 15-min; WS pushes trigger earlier
  });

  const { status, last } = useWebSocket("corr", 10);
  const lastComputed = (last as CorrWsMessage | undefined)?.computed_at;
  useEffect(() => {
    if (lastComputed && lastComputed !== data?.computed_at) {
      queryClient.invalidateQueries({ queryKey: ["corr"] });
    }
  }, [lastComputed, data?.computed_at, queryClient]);

  const warming = !data || data.cards.length === 0;

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

  const counts = data ? countStatuses(data.cards) : null;

  return (
    <div
      className="panel panel-expandable"
      onMouseDownCapture={onPanelMouseDown}
      onClick={onPanelClick}
      title="Click to expand"
    >
      <div className="panel-head">
        <span>Correlation Cookbook</span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className={`badge ${status === "open" ? "live" : "closed"}`}>
            {status === "open" ? "WS live" : status}
          </span>
          <button
            className="expand-btn"
            title="Expand: all 12 rule-cards + heatmap + per-card drill-down"
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
            warming up — first cookbook recompute lands ~6 min after boot
          </div>
        )}

        {data && !warming && counts && (
          <>
            <div
              className="regime-banner"
              style={{ borderColor: REGIME_COLOR[data.regime] ?? "var(--panel-border)" }}
            >
              <span
                className="regime-tag"
                style={{ color: REGIME_COLOR[data.regime] ?? "var(--text-dim)" }}
              >
                {data.regime.toUpperCase()}
                {data.stress.on && <span style={{ color: "var(--red)" }}> · STRESS</span>}
              </span>
              <span className="regime-score" style={{ fontSize: 11 }}>
                <span style={{ color: "var(--red)" }}>{counts.broken} broken</span>
                <span style={{ color: "var(--text-dim)" }}> · </span>
                <span style={{ color: "var(--yellow)" }}>{counts.watch} watch</span>
                <span style={{ color: "var(--text-dim)" }}> · </span>
                <span style={{ color: "var(--green)" }}>{counts.holds} hold</span>
              </span>
            </div>

            <div className="corr-grid">
              {data.cards.map((c) => (
                <CardTile
                  key={c.id}
                  c={c}
                  onPick={(id) => {
                    setDrillTo(id);
                    setExpanded(true);
                  }}
                />
              ))}
            </div>
          </>
        )}
      </div>
      {expanded && data && !warming && (
        <CorrDetail
          data={data}
          initialDrill={drillTo}
          onClose={() => {
            setExpanded(false);
            setDrillTo(null);
          }}
        />
      )}
    </div>
  );
}

/** Expanded view; a compact-tile click lands directly on that card's drill. */
function CorrDetail({
  data,
  initialDrill,
  onClose,
}: {
  data: CorrResponse;
  initialDrill: string | null;
  onClose: () => void;
}) {
  const [drill, setDrill] = useState<string | null>(initialDrill);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const counts = countStatuses(data.cards);
  const modal = (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>
            Correlation Cookbook
            <span style={{ color: REGIME_COLOR[data.regime] ?? "var(--text-dim)", marginLeft: 10 }}>
              {data.regime.toUpperCase()}
            </span>
            {data.stress.on && (
              <span style={{ color: "var(--red)", marginLeft: 10 }}>⚠ STRESS RADAR</span>
            )}
          </span>
          <button className="expand-btn" title="Close (Esc)" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          {drill ? (
            <CorrDrill cardId={drill} onBack={() => setDrill(null)} />
          ) : (
            <>
              <div className="stat-row">
                <div className="stat">
                  <div className="stat-value" style={{ color: "var(--red)" }}>{counts.broken}</div>
                  <div className="stat-label">broken</div>
                </div>
                <div className="stat">
                  <div className="stat-value" style={{ color: "var(--yellow)" }}>{counts.watch}</div>
                  <div className="stat-label">watch</div>
                </div>
                <div className="stat">
                  <div className="stat-value" style={{ color: "var(--green)" }}>{counts.holds}</div>
                  <div className="stat-label">holds</div>
                </div>
                <div className="stat">
                  <div className="stat-value">
                    {data.computed_at
                      ? new Date(data.computed_at).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })
                      : "—"}
                  </div>
                  <div className="stat-label">computed</div>
                </div>
              </div>

              <div className="chip-row" style={{ margin: "6px 0" }}>
                {data.stress.lights.map((l) => (
                  <span
                    key={l.key}
                    className="filing-chip"
                    style={{ color: l.on ? "var(--red)" : "var(--text-dim)" }}
                    title={`${l.label}: ${l.value ?? "—"}`}
                  >
                    {l.on ? "●" : "○"} {l.label}
                  </span>
                ))}
              </div>

              <div className="corr-grid full">
                {data.cards.map((c) => (
                  <CardTile key={c.id} c={c} onPick={setDrill} full />
                ))}
              </div>

              <Heatmap data={data} />
            </>
          )}
        </div>
      </div>
    </div>
  );
  return createPortal(modal, document.body);
}
