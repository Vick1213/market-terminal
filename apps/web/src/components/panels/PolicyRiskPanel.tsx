"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchPolicyRisk, runPolicyRisk } from "@/lib/api";

function num(n: number | undefined, digits = 0): string {
  return n == null ? "—" : n.toFixed(digits);
}

// These indices have no fixed scale; ~100 is the historical norm, so flag
// elevated readings hot relative to that baseline.
function levelColor(n: number | undefined, hot = 150, warm = 110): string {
  if (n == null) return "var(--text-dim)";
  if (n >= hot) return "var(--red)";
  if (n >= warm) return "var(--yellow)";
  return "var(--green)";
}

function Stat({
  label,
  value,
  color,
  sub,
}: {
  label: string;
  value: string;
  color?: string;
  sub?: string;
}) {
  return (
    <div style={{ minWidth: 92 }}>
      <div style={{ fontSize: 9, color: "var(--text-dim)" }}>{label}</div>
      <div className="num" style={{ fontSize: 15, color: color ?? "var(--text)" }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 9, color: "var(--text-dim)" }}>{sub}</div>}
    </div>
  );
}

function trend(latest: number | undefined, prior: number | undefined): string {
  if (latest == null || prior == null) return "";
  const d = latest - prior;
  if (Math.abs(d) < 0.5) return "→ flat";
  return d > 0 ? `▲ +${d.toFixed(0)}` : `▼ ${d.toFixed(0)}`;
}

export function PolicyRiskPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["policy-risk"],
    queryFn: () => fetchPolicyRisk(120),
    refetchInterval: 30 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: runPolicyRisk,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["policy-risk"] }),
  });

  const gpr = q.data?.gpr ?? [];
  const tpu = q.data?.tpu ?? [];
  const g = gpr[gpr.length - 1];
  const gPrev = gpr[gpr.length - 4]; // ~3 months earlier (monthly cadence)
  const t = tpu[tpu.length - 1];
  const tPrev = tpu[tpu.length - 22]; // ~1 month earlier (daily cadence)
  const hasData = !!(g || t);

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Policy-Risk Surface — GPR &amp; TPU</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          style={{ fontSize: 10 }}
          title="Pull the latest geopolitical-risk + trade-policy-uncertainty indices now"
        >
          {refresh.isPending ? "…" : "refresh"}
        </button>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && !hasData && (
          <div style={{ color: "var(--text-dim)" }}>no policy-risk data yet — refreshes every 12h</div>
        )}

        {hasData && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 20 }}>
            {g && (
              <div style={{ flex: 1, minWidth: 220 }}>
                <div className="macro-detail-title">
                  Geopolitical risk {g.date ? `· ${g.date}` : ""}
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
                  <Stat label="GPR" value={num(g.gpr)} color={levelColor(g.gpr)} sub={trend(g.gpr, gPrev?.gpr)} />
                  <Stat label="Threats" value={num(g.threats)} color={levelColor(g.threats)} sub="leads acts 1–3m" />
                  <Stat label="Acts" value={num(g.acts)} color={levelColor(g.acts)} />
                </div>
              </div>
            )}
            {t && (
              <div style={{ flex: 1, minWidth: 160 }}>
                <div className="macro-detail-title">
                  Trade-policy uncertainty {t.date ? `· ${t.date}` : ""}
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
                  <Stat
                    label="TPU"
                    value={num(t.tpu)}
                    color={levelColor(t.tpu, 200, 130)}
                    sub={trend(t.tpu, tPrev?.tpu)}
                  />
                </div>
              </div>
            )}
          </div>
        )}

        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 10 }}>
          GPR (monthly, ~100 = norm): geopolitical-risk headline + Threats (leading) / Acts. TPU
          (daily): trade-policy uncertainty. Trend vs ~3 months (GPR) / ~1 month (TPU) ago. Elevated
          readings (red) precede risk-off / vol spikes.
        </div>
      </div>
    </div>
  );
}
