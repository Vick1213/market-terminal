"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  ReportCardResponse,
  StrategistBucket,
  StrategistHolding,
  StrategistSignal,
} from "@market/shared";
import { fetchStrategist, fetchStrategistReport, runStrategist } from "@/lib/api";

const REGIME_COLOR: Record<string, string> = {
  "risk-on": "var(--green)",
  neutral: "var(--yellow)",
  "risk-off": "var(--red)",
  stress: "var(--red)",
};

const BUCKET_COLOR: Record<string, string> = {
  equities: "var(--green)",
  metals: "var(--yellow)",
  crypto: "var(--accent, #7aa2f7)",
  cash: "var(--text-dim)",
};

function BucketRow({ b, open, onToggle }: {
  b: StrategistBucket;
  open: boolean;
  onToggle: () => void;
}) {
  const tilt = b.weight_pct - b.base_pct;
  return (
    <div style={{ marginBottom: 4 }}>
      <div
        style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 12 }}
        onClick={onToggle}
        title={`base ${b.base_pct}% for this regime, signals tilt it ${tilt >= 0 ? "+" : ""}${tilt.toFixed(1)}pp. Click for the why.`}
      >
        <span style={{ minWidth: 12, color: "var(--text-dim)" }}>{open ? "▾" : "▸"}</span>
        <span style={{ flex: "0 0 130px" }}>{b.label}</span>
        <div style={{ flex: 1, height: 8, background: "var(--bg-2, #1c1c1c)", borderRadius: 3 }}>
          <div
            style={{
              width: `${Math.min(100, b.weight_pct)}%`,
              height: "100%",
              borderRadius: 3,
              background: BUCKET_COLOR[b.key] ?? "var(--text-dim)",
              opacity: 0.8,
            }}
          />
        </div>
        <span className="num" style={{ minWidth: 44, textAlign: "right" }}>
          {b.weight_pct.toFixed(0)}%
        </span>
        <span
          className="num"
          style={{
            minWidth: 40,
            textAlign: "right",
            fontSize: 10,
            color: tilt > 0.5 ? "var(--green)" : tilt < -0.5 ? "var(--red)" : "var(--text-dim)",
          }}
          title="tilt vs the regime base"
        >
          {tilt > 0.5 || tilt < -0.5 ? `${tilt > 0 ? "+" : ""}${tilt.toFixed(0)}pp` : "base"}
        </span>
      </div>
      {open && (
        <div style={{ padding: "2px 0 4px 18px", fontSize: 11, color: "var(--text-dim)" }}>
          {b.reasons.map((r, i) => (
            <div key={i} style={{ display: "flex", gap: 6 }}>
              <span
                className="num"
                style={{
                  minWidth: 34,
                  color: r.delta > 0 ? "var(--green)" : r.delta < 0 ? "var(--red)" : "var(--text-dim)",
                }}
              >
                {r.delta === 0 ? "—" : `${r.delta > 0 ? "+" : ""}${r.delta}`}
              </span>
              <span>{r.detail}</span>
            </div>
          ))}
          {!!b.holdings?.length && (
            <>
              <div style={{ margin: "5px 0 2px", textTransform: "uppercase", fontSize: 10, letterSpacing: 0.5 }}>
                inside this sleeve
              </div>
              {b.holdings.map((h) => (
                <HoldingRow key={`${h.kind}:${h.symbol}`} h={h} />
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function HoldingRow({ h }: { h: StrategistHolding }) {
  return (
    <div
      style={{ display: "flex", gap: 6, alignItems: "baseline", minWidth: 0 }}
      title={h.evidence.join("\n")}
    >
      <span className="num" style={{ minWidth: 34, textAlign: "right" }}>
        {h.sleeve_pct.toFixed(0)}%
      </span>
      <span
        style={{
          minWidth: 44,
          color: h.kind === "stock" ? "var(--text, #ddd)" : undefined,
          fontWeight: h.kind === "stock" ? 600 : 400,
        }}
      >
        {h.symbol}
      </span>
      <span className="num" style={{ minWidth: 52, fontSize: 10 }} title="share of the whole portfolio">
        {h.weight_pct.toFixed(1)}% pf
      </span>
      <span
        style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 10 }}
      >
        {h.kind === "stock" && h.score != null ? `score ${h.score} · ` : ""}
        {h.evidence[0]}
        {h.evidence.length > 1 ? ` (+${h.evidence.length - 1} more — hover)` : ""}
      </span>
    </div>
  );
}

const fmtPct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;

const retColor = (v: number | null | undefined) =>
  v === null || v === undefined || Math.abs(v) < 0.05
    ? "var(--text-dim)"
    : v > 0
      ? "var(--green)"
      : "var(--red)";

function ReportCard({ r }: { r: ReportCardResponse }) {
  const cell: React.CSSProperties = { padding: "1px 6px", textAlign: "right" };
  return (
    <div style={{ marginTop: 8, fontSize: 11 }}>
      <div className="macro-detail-title">Report card — is the engine right?</div>

      <div style={{ color: "var(--text-dim)", marginBottom: 4 }}>
        {r.summary.scored > 0 ? (
          <>
            {r.summary.scored}/{r.summary.snapshots} snapshots scored · avg 1w excess vs SPY{" "}
            <span style={{ color: retColor(r.summary.avg_excess_1w) }}>
              {fmtPct(r.summary.avg_excess_1w)}
            </span>
            {" · "}beat SPY {r.summary.hit_rate_1w}% of weeks
          </>
        ) : (
          <>
            {r.summary.snapshots} snapshot(s) stored, none old enough to score yet (needs 1w+
            of forward bars) — the regime backtest below replays the classifier over stored
            macro history in the meantime.
          </>
        )}
      </div>

      {r.regimes.length > 0 && (
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead>
            <tr style={{ color: "var(--text-dim)" }}>
              <th style={{ ...cell, textAlign: "left" }}>regime</th>
              <th style={cell}>days</th>
              <th style={cell} title="avg forward return of the regime's base allocation">alloc 1w</th>
              <th style={cell}>SPY 1w</th>
              <th style={cell}>alloc 1m</th>
              <th style={cell}>SPY 1m</th>
            </tr>
          </thead>
          <tbody>
            {r.regimes.map((g) => (
              <tr key={g.regime}>
                <td style={{ ...cell, textAlign: "left" }}>{g.regime}</td>
                <td className="num" style={cell}>{g.days}</td>
                <td className="num" style={{ ...cell, color: retColor(g.alloc_1w) }}>{fmtPct(g.alloc_1w)}</td>
                <td className="num" style={{ ...cell, color: retColor(g.spy_1w) }}>{fmtPct(g.spy_1w)}</td>
                <td className="num" style={{ ...cell, color: retColor(g.alloc_1m) }}>{fmtPct(g.alloc_1m)}</td>
                <td className="num" style={{ ...cell, color: retColor(g.spy_1m) }}>{fmtPct(g.spy_1m)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {r.signals.length > 0 && (
        <>
          <div className="macro-detail-title" style={{ marginTop: 6 }}>
            Signal hit rates (SPY 1w after fired vs quiet)
          </div>
          {r.signals.map((s) => (
            <div key={s.signal} style={{ display: "flex", gap: 8 }}>
              <span style={{ flex: "0 0 110px" }}>{s.signal}</span>
              <span className="num">fired {s.fired}×</span>
              <span className="num" style={{ color: retColor(s.spy_1w_after_fired) }}>
                {fmtPct(s.spy_1w_after_fired)}
              </span>
              <span style={{ color: "var(--text-dim)" }}>vs</span>
              <span className="num" style={{ color: retColor(s.spy_1w_after_quiet) }}>
                {fmtPct(s.spy_1w_after_quiet)}
              </span>
            </div>
          ))}
        </>
      )}

      <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 6 }}>{r.note}</div>
    </div>
  );
}

function SignalChip({ s }: { s: StrategistSignal }) {
  return (
    <span
      style={{
        fontSize: 10,
        padding: "1px 6px",
        borderRadius: 3,
        border: "1px solid var(--border, #2a2a2a)",
        color: s.stale ? "var(--text-dim)" : "var(--text, #ddd)",
        opacity: s.stale ? 0.6 : 1,
      }}
      title={`${s.label}: ${s.detail}${s.asof ? ` (as of ${s.asof})` : ""}${
        s.stale ? " — STALE/MISSING, excluded from the rules" : ""
      }`}
    >
      {s.stale ? "○" : "●"} {s.label}
      {s.value !== null && s.value !== undefined ? `: ${s.value}` : ""}
    </span>
  );
}

export function StrategistPanel() {
  const queryClient = useQueryClient();
  const q = useQuery({
    queryKey: ["strategist"],
    queryFn: fetchStrategist,
    refetchInterval: 30 * 60_000,
  });
  const rerun = useMutation({
    mutationFn: runStrategist,
    onSuccess: (data) => queryClient.setQueryData(["strategist"], data),
  });
  const [openBucket, setOpenBucket] = useState<string | null>(null);
  const [showReport, setShowReport] = useState(false);
  const report = useQuery({
    queryKey: ["strategist-report"],
    queryFn: fetchStrategistReport,
    enabled: showReport,
    staleTime: 30 * 60_000,
  });

  const d = q.data;
  const warming = !d || d.status === "warming-up" || !d.buckets;

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Strategist</span>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {d?.model && (
            <span style={{ fontSize: 10, color: "var(--text-dim)" }} title="narrative source">
              {d.model}
            </span>
          )}
          <button
            className="expand-btn"
            title="Report card: forward-return scoring of past suggestions + regime backtest"
            onClick={(e) => {
              e.stopPropagation();
              setShowReport((v) => !v);
            }}
            style={showReport ? { color: "var(--green)" } : undefined}
          >
            ✓?
          </button>
          <button
            className="expand-btn"
            title="Recompute now (re-reads every stored signal)"
            onClick={(e) => {
              e.stopPropagation();
              rerun.mutate();
            }}
            disabled={rerun.isPending}
          >
            {rerun.isPending ? "…" : "↻"}
          </button>
        </span>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && warming && (
          <div style={{ color: "var(--text-dim)" }}>
            warming up — the strategist runs ~12 min after boot, or hit ↻
          </div>
        )}

        {!warming && d && (
          <>
            <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 8 }}>
              <span
                style={{
                  fontWeight: 700,
                  color: REGIME_COLOR[d.regime ?? ""] ?? "var(--text, #ddd)",
                }}
              >
                {(d.regime ?? "unknown").toUpperCase()}
              </span>
              {typeof d.score === "number" && (
                <span className="num" style={{ fontSize: 11, color: "var(--text-dim)" }}>
                  composite {d.score > 0 ? "+" : ""}
                  {d.score}
                </span>
              )}
              <span style={{ fontSize: 10, color: "var(--text-dim)", marginLeft: "auto" }}>
                as of {d.as_of?.replace("T", " ")}
              </span>
            </div>

            <div className="macro-detail-title">Suggested allocation (click a row for why)</div>
            {d.buckets!.map((b) => (
              <BucketRow
                key={b.key}
                b={b}
                open={openBucket === b.key}
                onToggle={() => setOpenBucket(openBucket === b.key ? null : b.key)}
              />
            ))}

            {d.equity_tilt && (d.equity_tilt.favor.length > 0 || d.equity_tilt.avoid.length > 0) && (
              <div style={{ fontSize: 11, margin: "6px 0" }}>
                <span style={{ color: "var(--text-dim)" }}>equity sleeve (RRG): </span>
                {d.equity_tilt.favor.length > 0 && (
                  <span style={{ color: "var(--green)" }}>favor {d.equity_tilt.favor.join(", ")}</span>
                )}
                {d.equity_tilt.favor.length > 0 && d.equity_tilt.avoid.length > 0 && " · "}
                {d.equity_tilt.avoid.length > 0 && (
                  <span style={{ color: "var(--red)" }}>avoid {d.equity_tilt.avoid.join(", ")}</span>
                )}
              </div>
            )}

            {!!d.notes?.length && (
              <>
                <div className="macro-detail-title" style={{ marginTop: 8 }}>Strategy notes</div>
                {d.notes.map((n, i) => (
                  <div key={i} style={{ fontSize: 11, marginBottom: 3 }}>
                    – {n}
                  </div>
                ))}
              </>
            )}

            {!!d.signals?.length && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 8 }}>
                {d.signals.map((s) => (
                  <SignalChip key={s.key} s={s} />
                ))}
              </div>
            )}

            {showReport && report.isLoading && (
              <div style={{ color: "var(--text-dim)", marginTop: 8 }}>scoring history…</div>
            )}
            {showReport && report.data && <ReportCard r={report.data} />}

            <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 8 }}>
              {d.disclaimer}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
