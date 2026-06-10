"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { RrgQuadrant, RrgSector } from "@market/shared";
import { fetchRotation } from "@/lib/api";

const QUAD_COLOR: Record<RrgQuadrant, string> = {
  leading: "var(--green)",
  weakening: "var(--yellow)",
  lagging: "var(--red)",
  improving: "var(--blue, #4ea1ff)",
};

const MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** RRG scatter: RS-Ratio (x) vs RS-Momentum (y), quadrants around (100,100). */
function RrgChart({ sectors, size }: { sectors: RrgSector[]; size: number }) {
  const [hover, setHover] = useState<string | null>(null);
  // Scale spans the data plus padding, centered on 100/100.
  const all = sectors.flatMap((s) => [
    ...s.trail.map((t) => [t.r, t.m]),
    [s.rs_ratio, s.rs_momentum],
  ]);
  const span = Math.max(
    2.5,
    ...all.map(([r, m]) => Math.max(Math.abs(r - 100), Math.abs(m - 100)))
  ) * 1.15;
  const x = (v: number) => ((v - (100 - span)) / (2 * span)) * size;
  const y = (v: number) => size - ((v - (100 - span)) / (2 * span)) * size;

  return (
    <svg width="100%" viewBox={`0 0 ${size} ${size}`} style={{ display: "block" }}>
      {/* quadrant tints */}
      <rect x={x(100)} y={0} width={size - x(100)} height={y(100)} fill="rgba(60,180,90,.07)" />
      <rect x={x(100)} y={y(100)} width={size - x(100)} height={size - y(100)} fill="rgba(220,180,40,.06)" />
      <rect x={0} y={y(100)} width={x(100)} height={size - y(100)} fill="rgba(220,70,70,.07)" />
      <rect x={0} y={0} width={x(100)} height={y(100)} fill="rgba(80,140,255,.06)" />
      <line x1={x(100)} y1={0} x2={x(100)} y2={size} stroke="var(--border, #333)" strokeWidth={1} />
      <line x1={0} y1={y(100)} x2={size} y2={y(100)} stroke="var(--border, #333)" strokeWidth={1} />
      <text x={size - 4} y={12} textAnchor="end" fontSize={9} fill="var(--green)">LEADING</text>
      <text x={size - 4} y={size - 5} textAnchor="end" fontSize={9} fill="var(--yellow)">WEAKENING</text>
      <text x={4} y={size - 5} fontSize={9} fill="var(--red)">LAGGING</text>
      <text x={4} y={12} fontSize={9} fill="var(--blue, #4ea1ff)">IMPROVING</text>

      {sectors.map((s) => {
        const c = QUAD_COLOR[s.quadrant];
        const pts = [...s.trail.map((t) => [t.r, t.m]), [s.rs_ratio, s.rs_momentum]];
        const dim = hover !== null && hover !== s.symbol;
        return (
          <g
            key={s.symbol}
            opacity={dim ? 0.18 : 1}
            onMouseEnter={() => setHover(s.symbol)}
            onMouseLeave={() => setHover(null)}
            style={{ cursor: "default" }}
          >
            <title>{`${s.symbol} ${s.name} — ratio ${s.rs_ratio}, momentum ${s.rs_momentum} (${s.quadrant})`}</title>
            <polyline
              points={pts.map(([r, m]) => `${x(r)},${y(m)}`).join(" ")}
              fill="none"
              stroke={c}
              strokeWidth={1}
              strokeOpacity={0.55}
            />
            {s.trail.map((t, i) => (
              <circle key={i} cx={x(t.r)} cy={y(t.m)} r={1.6} fill={c} fillOpacity={0.5} />
            ))}
            <circle cx={x(s.rs_ratio)} cy={y(s.rs_momentum)} r={4} fill={c} />
            <text
              x={x(s.rs_ratio) + 6}
              y={y(s.rs_momentum) + 3}
              fontSize={10}
              fill="var(--text, #ddd)"
            >
              {s.symbol}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export function RotationPanel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["rotation"],
    queryFn: fetchRotation,
    refetchInterval: 30 * 60_000, // recomputed server-side from daily bars
  });

  const warming = !data || data.sectors.length === 0;

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Sector Rotation (RRG vs {data?.benchmark ?? "SPY"})</span>
        <span style={{ fontSize: 10, color: "var(--text-dim)" }}>daily · 8wk trail</span>
      </div>
      <div className="panel-body">
        {isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}
        {!isLoading && warming && !isError && (
          <div style={{ color: "var(--text-dim)" }}>
            warming up — sector ETF history lands within ~8 min of boot
          </div>
        )}
        {data && !warming && (
          <>
            <RrgChart sectors={data.sectors} size={340} />
            {data.seasonality.length > 0 && (
              <>
                <div className="macro-detail-title" style={{ marginTop: 10 }}>
                  Seasonality — watchlist, this month vs next
                </div>
                <table className="wl-table">
                  <thead>
                    <tr>
                      <th>symbol</th>
                      {data.seasonality[0]?.months.map((m) => (
                        <th key={m.month} className="num" colSpan={2}>
                          {MONTHS[m.month]}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.seasonality.map((row) => (
                      <tr key={row.symbol} className="wl-row">
                        <td className="wl-sym">{row.symbol}</td>
                        {row.months.map((m) => (
                          <td
                            key={m.month}
                            className="num"
                            colSpan={2}
                            title={`${MONTHS[m.month]}: ${m.mean_return >= 0 ? "+" : ""}${m.mean_return}% avg over ${m.years}y, up ${m.win_rate}% of years`}
                            style={{ color: m.mean_return >= 0 ? "var(--green)" : "var(--red)" }}
                          >
                            {m.mean_return >= 0 ? "+" : ""}
                            {m.mean_return}% · {m.win_rate}%
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
