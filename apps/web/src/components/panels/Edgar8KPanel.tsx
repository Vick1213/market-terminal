"use client";

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Edgar8KEvent } from "@market/shared";
import { fetchEdgar8K, runEdgar8K } from "@/lib/api";

// Each item code is a distinct tradeable catalyst; colour by how the market
// usually reads it (cyber/impairment = bad, departure = uncertainty, buyback =
// supportive). The short tag is what shows on the row badge.
const CATALYST: Record<string, { tag: string; color: string }> = {
  "1.05": { tag: "CYBER", color: "var(--red)" },
  "2.06": { tag: "IMPAIR", color: "var(--yellow)" },
  "5.02": { tag: "EXEC", color: "var(--yellow)" },
  "8.01": { tag: "BUYBACK", color: "var(--green)" },
};

function meta(code: string) {
  return CATALYST[code] ?? { tag: code, color: "var(--text-dim)" };
}

function EventRow({ e }: { e: Edgar8KEvent }) {
  const m = meta(e.item_code);
  const title = `${e.catalyst} (item ${e.item_code})\n${e.company ?? ""}\nfiled ${e.filed_at} · items ${e.items.join(", ")}`;
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
        <span className="wl-sym">{e.ticker ?? "—"}</span>
      </td>
      <td style={{ maxWidth: 150, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {e.url ? (
          <a href={e.url} target="_blank" rel="noreferrer" style={{ color: "inherit" }}>
            {e.company ?? e.accession}
          </a>
        ) : (
          e.company ?? e.accession
        )}
      </td>
      <td className="num" style={{ color: "var(--text-dim)" }}>
        {e.filed_at.slice(5)}
      </td>
    </tr>
  );
}

export function Edgar8KPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["edgar8k"],
    queryFn: () => fetchEdgar8K(60),
    refetchInterval: 10 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: runEdgar8K,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["edgar8k"] }),
  });

  const events = q.data?.events ?? [];
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const e of events) c[e.item_code] = (c[e.item_code] ?? 0) + 1;
    return c;
  }, [events]);

  return (
    <div className="panel">
      <div className="panel-head">
        <span>8-K Catalysts — Corporate Events</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          style={{ fontSize: 10 }}
          title="Scan EDGAR full-text search for new 8-K item-code filings now"
        >
          {refresh.isPending ? "…" : "refresh"}
        </button>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && !events.length && (
          <div style={{ color: "var(--text-dim)" }}>
            no recent 8-K catalysts — the scanner sweeps EDGAR every few hours
          </div>
        )}

        {!!events.length && (
          <>
            {/* Catalyst legend + live counts */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, fontSize: 10, marginBottom: 6 }}>
              {Object.entries(CATALYST).map(([code, m]) => (
                <span key={code} style={{ color: counts[code] ? m.color : "var(--text-dim)" }}>
                  {m.tag} {counts[code] ?? 0}
                </span>
              ))}
            </div>
            <table className="wl-table">
              <thead>
                <tr>
                  <th>type</th>
                  <th>sym</th>
                  <th>company</th>
                  <th className="num">filed</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <EventRow key={e.accession} e={e} />
                ))}
              </tbody>
            </table>
          </>
        )}

        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 8 }}>
          1.05 cyber incident · 2.06 impairment · 5.02 exec departure · 8.01 buyback. Sourced from
          EDGAR full-text search; each catalyst confirmed against the filing&apos;s structured item codes.
        </div>
      </div>
    </div>
  );
}
