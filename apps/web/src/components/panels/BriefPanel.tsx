"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { BriefWsMessage } from "@market/shared";
import { fetchBrief, runBrief } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";

/** Tiny markdown subset: **bold**, - bullets, blank-line paragraphs. */
function Markdown({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/);
  return (
    <div style={{ fontSize: 12, lineHeight: 1.5 }}>
      {blocks.map((block, bi) => (
        <div key={bi} style={{ marginBottom: 8 }}>
          {block.split("\n").map((line, li) => {
            const bullet = line.startsWith("- ");
            const parts = (bullet ? line.slice(2) : line).split(/(\*\*[^*]+\*\*)/g);
            const rendered = parts.map((p, pi) =>
              p.startsWith("**") && p.endsWith("**") ? (
                <strong key={pi} style={{ color: "var(--text, #ddd)" }}>
                  {p.slice(2, -2)}
                </strong>
              ) : (
                <span key={pi}>{p.replace(/^#+\s*/, "")}</span>
              )
            );
            return bullet ? (
              <div key={li} style={{ paddingLeft: 12, textIndent: -8 }}>• {rendered}</div>
            ) : (
              <div key={li}>{rendered}</div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

export function BriefPanel() {
  const queryClient = useQueryClient();
  const [running, setRunning] = useState(false);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["brief"],
    queryFn: fetchBrief,
    refetchInterval: 30 * 60_000,
  });

  const { last } = useWebSocket("brief", 5);
  useEffect(() => {
    if ((last as BriefWsMessage | undefined)?.type === "brief") {
      queryClient.invalidateQueries({ queryKey: ["brief"] });
    }
  }, [last, queryClient]);

  const onRun = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setRunning(true);
    try {
      await runBrief();
      await queryClient.invalidateQueries({ queryKey: ["brief"] });
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Morning Brief{data?.date ? ` — ${data.date}` : ""}</span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {data?.model && (
            <span
              className="filing-chip"
              style={{ color: data.model === "template" ? "var(--yellow)" : "var(--green)" }}
              title={
                data.model === "template"
                  ? "deterministic fallback — start Ollama for the LLM narrative"
                  : `written by local ${data.model} ($0 tokens)`
              }
            >
              {data.model}
            </span>
          )}
          <button className="expand-btn" onClick={onRun} title="Regenerate now" disabled={running}>
            {running ? "…" : "↻"}
          </button>
        </span>
      </div>
      <div className="panel-body">
        {isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}
        {data && !data.text && !isLoading && (
          <div style={{ color: "var(--text-dim)" }}>
            no brief yet — runs pre-market daily, or hit ↻ to generate one now
          </div>
        )}
        {data?.text && <Markdown text={data.text} />}
      </div>
    </div>
  );
}
