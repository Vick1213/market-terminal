"use client";

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Edgar13DFiling } from "@market/shared";
import { fetchEdgar13D, runEdgar13D } from "@/lib/api";

// 13D = activist (intent to influence — moves the target fast); 13G = passive.
// Amendments (/A) inherit the base form's stance. Colour activist filings hot
// (13D red, 13D/A yellow) and passive ones dim.
function meta(f: Edgar13DFiling): { tag: string; color: string; note: string } {
  if (f.is_activist) {
    const amend = f.filing_type.includes("/A");
    return {
      tag: amend ? "13D/A" : "13D",
      color: amend ? "var(--yellow)" : "var(--red)",
      note: "intent to influence",
    };
  }
  return { tag: f.filing_type.includes("/A") ? "13G/A" : "13G", color: "var(--text-dim)", note: "passive" };
}

function fmtPct(p: number | null): string {
  return p == null ? "—" : `${p.toFixed(1)}%`;
}

function FilingRow({ f }: { f: Edgar13DFiling }) {
  const m = meta(f);
  const title = [
    `${f.filing_type} — ${m.note}`,
    f.purpose ? `purpose: ${f.purpose}` : null,
    `filed ${f.filed_at}`,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <tr className="wl-row" title={title}>
      <td>
        <span
          style={{
            fontSize: 9,
            fontWeight: 700,
            padding: "1px 4px",
            borderRadius: 3,
            background: "var(--bg-2, #1a1a1a)",
            color: m.color,
            whiteSpace: "nowrap",
          }}
        >
          {m.tag}
        </span>
      </td>
      <td>
        <span className="wl-sym">{f.subject_ticker ?? "—"}</span>
      </td>
      <td style={{ maxWidth: 130, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {f.url ? (
          <a href={f.url} target="_blank" rel="noreferrer" style={{ color: "inherit" }}>
            {f.subject_name ?? f.accession}
          </a>
        ) : (
          f.subject_name ?? f.accession
        )}
      </td>
      <td style={{ maxWidth: 110, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-dim)" }}>
        {f.filer_name ?? "—"}
      </td>
      <td className="num">{fmtPct(f.pct_owned)}</td>
    </tr>
  );
}

export function Edgar13DPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["edgar13d"],
    queryFn: () => fetchEdgar13D(60),
    refetchInterval: 10 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: runEdgar13D,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["edgar13d"] }),
  });

  const filings = q.data?.filings ?? [];
  const counts = useMemo(() => {
    let activist = 0;
    let passive = 0;
    for (const f of filings) (f.is_activist ? (activist += 1) : (passive += 1));
    return { activist, passive };
  }, [filings]);

  return (
    <div className="panel">
      <div className="panel-head">
        <span>13D / 13G — Activist & Passive Stakes</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          style={{ fontSize: 10 }}
          title="Scan EDGAR full-text search for new SC 13D/13G filings now"
        >
          {refresh.isPending ? "…" : "refresh"}
        </button>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && !filings.length && (
          <div style={{ color: "var(--text-dim)" }}>
            no recent 13D/13G filings — the scanner sweeps EDGAR every few hours
          </div>
        )}

        {!!filings.length && (
          <>
            {/* Activist vs passive legend + live counts */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 12, fontSize: 10, marginBottom: 6 }}>
              <span style={{ color: counts.activist ? "var(--red)" : "var(--text-dim)" }}>
                13D activist {counts.activist}
              </span>
              <span style={{ color: "var(--text-dim)" }}>13G passive {counts.passive}</span>
            </div>
            <table className="wl-table">
              <thead>
                <tr>
                  <th>type</th>
                  <th>sym</th>
                  <th>subject</th>
                  <th>filer</th>
                  <th className="num">owned</th>
                </tr>
              </thead>
              <tbody>
                {filings.map((f) => (
                  <FilingRow key={f.accession} f={f} />
                ))}
              </tbody>
            </table>
          </>
        )}

        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 8 }}>
          13D = intent to influence (moves target fast); 13G = passive. % / shares / purpose from the
          Dec-2024 structured XML, null for older HTML-only filings.
        </div>
      </div>
    </div>
  );
}
