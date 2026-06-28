"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { FedSpeech } from "@market/shared";
import { fetchFedSpeeches, runFedSpeeches } from "@/lib/api";

// hawk_dove is a FinBERT-sentiment proxy: >0 hawkish (restrictive tone reads
// negative → hawk), <0 dovish. Colour and label by direction.
function hd(v: number): { tag: string; color: string } {
  if (v > 0.15) return { tag: "HAWK", color: "var(--red)" };
  if (v < -0.15) return { tag: "DOVE", color: "var(--green)" };
  return { tag: "NEUT", color: "var(--text-dim)" };
}

function SpeechRow({ s }: { s: FedSpeech }) {
  const m = hd(s.hawk_dove);
  const title = `${s.title}\n${s.speaker} · ${s.date}\nhawk/dove ${s.hawk_dove.toFixed(2)} (sentiment ${s.score.toFixed(2)}, conf ${s.confidence.toFixed(2)}, ${s.chunks} chunks)`;
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
      <td style={{ maxWidth: 90, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        <span className="wl-sym">{s.speaker}</span>
      </td>
      <td style={{ maxWidth: 150, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {s.url ? (
          <a href={s.url} target="_blank" rel="noreferrer" style={{ color: "inherit" }}>
            {s.title}
          </a>
        ) : (
          s.title
        )}
      </td>
      <td className="num" style={{ color: m.color }}>
        {s.hawk_dove >= 0 ? "+" : ""}
        {s.hawk_dove.toFixed(2)}
      </td>
      <td className="num" style={{ color: "var(--text-dim)" }}>
        {s.date.slice(5)}
      </td>
    </tr>
  );
}

export function FedSpeechesPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["fed-speeches"],
    queryFn: () => fetchFedSpeeches(30),
    refetchInterval: 30 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: runFedSpeeches,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["fed-speeches"] }),
  });

  const speeches = q.data?.speeches ?? [];
  const index = q.data?.index ?? null;
  const idxMeta = index ? hd(index.value) : null;

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Fed Speeches — Hawk / Dove</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          style={{ fontSize: 10 }}
          title="Fetch + FinBERT-score the latest Fed speeches now"
        >
          {refresh.isPending ? "…" : "refresh"}
        </button>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && !speeches.length && (
          <div style={{ color: "var(--text-dim)" }}>no scored speeches yet — refreshes every 12h</div>
        )}

        {index && idxMeta && (
          <div style={{ fontSize: 11, marginBottom: 8 }}>
            rolling hawk/dove index:{" "}
            <span className="num" style={{ color: idxMeta.color }}>
              {index.value >= 0 ? "+" : ""}
              {index.value.toFixed(2)} {idxMeta.tag}
            </span>{" "}
            <span style={{ color: "var(--text-dim)", fontSize: 9 }}>as of {index.date}</span>
          </div>
        )}

        {!!speeches.length && (
          <table className="wl-table">
            <thead>
              <tr>
                <th>tone</th>
                <th>speaker</th>
                <th>title</th>
                <th className="num">h/d</th>
                <th className="num">date</th>
              </tr>
            </thead>
            <tbody>
              {speeches.map((s) => (
                <SpeechRow key={s.url} s={s} />
              ))}
            </tbody>
          </table>
        )}

        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 8 }}>
          Hawk/dove is a FinBERT-sentiment proxy (restrictive tone reads negative → hawkish): &gt;0
          hawkish, &lt;0 dovish. A directional tell, not a calibrated rate forecast. Index = rolling
          mean over recent speeches.
        </div>
      </div>
    </div>
  );
}
