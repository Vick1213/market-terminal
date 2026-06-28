"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ClevelandRow } from "@market/shared";
import { fetchClevelandNowcast, runClevelandNowcast } from "@/lib/api";

type MetricKey = "cpi" | "core_cpi" | "pce" | "core_pce";
const METRICS: { key: MetricKey; label: string }[] = [
  { key: "cpi", label: "CPI" },
  { key: "core_cpi", label: "Core CPI" },
  { key: "pce", label: "PCE" },
  { key: "core_pce", label: "Core PCE" },
];

function fmt(n: number | undefined): string {
  return n == null ? "—" : `${n.toFixed(2)}%`;
}

// nowcast vs the last realized print — a positive gap means the nowcast is
// running hotter than the last actual (upside surprise risk).
function gapColor(nowcast: number | undefined, actual: number | undefined): string {
  if (nowcast == null || actual == null) return "var(--text-dim)";
  const g = nowcast - actual;
  if (g > 0.05) return "var(--red)";
  if (g < -0.05) return "var(--green)";
  return "var(--text-dim)";
}

function MetricRow({
  label,
  mk,
  mom,
  yoy,
  momPrev,
}: {
  label: string;
  mk: MetricKey;
  mom: ClevelandRow | undefined;
  yoy: ClevelandRow | undefined;
  momPrev: ClevelandRow | undefined;
}) {
  const nc = mom?.[mk];
  const act = mom?.[`${mk}_actual` as keyof ClevelandRow] as number | undefined;
  const prev = momPrev?.[mk];
  const drift = nc != null && prev != null ? nc - prev : null;
  const ncYoy = yoy?.[mk];
  return (
    <tr
      className="wl-row"
      title={`${label} — MoM nowcast vs last actual; YoY nowcast ${fmt(ncYoy)}${
        drift != null ? `\nday-over-day drift ${drift >= 0 ? "+" : ""}${drift.toFixed(3)}` : ""
      }`}
    >
      <td>
        <span className="wl-sym">{label}</span>
      </td>
      <td className="num" style={{ color: gapColor(nc, act) }}>
        {fmt(nc)}
      </td>
      <td className="num" style={{ color: "var(--text-dim)" }}>
        {fmt(act)}
      </td>
      <td className="num">
        {drift == null ? (
          ""
        ) : (
          <span style={{ color: drift > 0 ? "var(--red)" : drift < 0 ? "var(--green)" : "var(--text-dim)" }}>
            {drift > 0 ? "▲" : drift < 0 ? "▼" : "·"}
          </span>
        )}
      </td>
      <td className="num" style={{ color: "var(--text-dim)" }}>
        {fmt(ncYoy)}
      </td>
    </tr>
  );
}

export function RatesInflationPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["cleveland-nowcast"],
    queryFn: () => fetchClevelandNowcast(120),
    refetchInterval: 30 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: runClevelandNowcast,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cleveland-nowcast"] }),
  });

  const mom = q.data?.mom ?? [];
  const yoy = q.data?.yoy ?? [];
  const latestMom = mom[mom.length - 1];
  const prevMom = mom[mom.length - 2];
  const latestYoy = yoy[yoy.length - 1];

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Rates &amp; Inflation — Cleveland Nowcast</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          style={{ fontSize: 10 }}
          title="Pull the latest Cleveland Fed inflation nowcast now"
        >
          {refresh.isPending ? "…" : "refresh"}
        </button>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && !latestMom && (
          <div style={{ color: "var(--text-dim)" }}>no nowcast data — updates daily ~10am ET</div>
        )}

        {latestMom && (
          <>
            <div style={{ fontSize: 9, color: "var(--text-dim)", marginBottom: 4 }}>
              as of {latestMom.date} · MoM nowcast vs last actual, with day-over-day drift
            </div>
            <table className="wl-table">
              <thead>
                <tr>
                  <th>metric</th>
                  <th className="num">MoM nc</th>
                  <th className="num">actual</th>
                  <th className="num">drift</th>
                  <th className="num">YoY nc</th>
                </tr>
              </thead>
              <tbody>
                {METRICS.map((m) => (
                  <MetricRow
                    key={m.key}
                    label={m.label}
                    mk={m.key}
                    mom={latestMom}
                    yoy={latestYoy}
                    momPrev={prevMom}
                  />
                ))}
              </tbody>
            </table>
          </>
        )}

        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 8 }}>
          Cleveland Fed nowcast updates daily. nc hotter than the last actual (red) = upside-surprise
          risk into the print; drift ▲/▼ is today&apos;s move vs yesterday. CPI series can be blank
          mid-cycle until Cleveland repopulates.
        </div>
      </div>
    </div>
  );
}
