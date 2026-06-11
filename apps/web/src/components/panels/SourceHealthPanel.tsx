"use client";

import { useQuery } from "@tanstack/react-query";
import type { SourceHealthItem, SourceStatus } from "@market/shared";
import { fetchSourceHealth } from "@/lib/api";

function statusColor(s: SourceStatus): string {
  if (s === "dead") return "var(--red)";
  if (s === "degraded") return "var(--yellow)";
  return "var(--green)";
}

function age(iso: string | null): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.floor(ms / 60_000);
  if (min < 1) return "now";
  if (min < 60) return `${min}m ago`;
  const h = Math.floor(min / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function SourceRow({ s }: { s: SourceHealthItem }) {
  const title = [
    s.covers && `feeds: ${s.covers}`,
    `${s.success_count} ok / ${s.failure_count} failed lifetime`,
    s.last_error && `last error: ${s.last_error}`,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <div className="news-item" title={title}>
      <div className="news-title">
        <span style={{ color: statusColor(s.status), marginRight: 6 }}>●</span>
        {s.label}
        {s.consecutive_failures > 0 && (
          <span style={{ color: statusColor(s.status), marginLeft: 6, fontSize: 11 }}>
            ×{s.consecutive_failures}
          </span>
        )}
      </div>
      <div className="news-meta">
        <span style={{ color: statusColor(s.status) }}>{s.status}</span>
        <span>{s.host}</span>
        <span>last ok {age(s.last_success)}</span>
      </div>
    </div>
  );
}

export function SourceHealthPanel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["sourceHealth"],
    queryFn: fetchSourceHealth,
    refetchInterval: 60_000,
  });

  // Dead/degraded first (the backend orders by streak), then alphabetical.
  const sources = data?.sources ?? [];
  const sick = sources.filter((s) => s.status !== "ok");
  const healthy = sources.filter((s) => s.status === "ok");

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Data Sources</span>
        {data && (
          <span
            className="badge"
            style={{ color: statusColor(data.status) }}
            title="worst status across all tracked hosts"
          >
            {data.status === "ok"
              ? `all ${sources.length} ok`
              : `${sick.length} ${data.status}`}
          </span>
        )}
      </div>
      <div className="panel-body">
        {isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}
        {data && sources.length === 0 && (
          <div style={{ color: "var(--text-dim)" }}>
            no fetches recorded yet — rows appear as ingestors run
          </div>
        )}
        {sources.length > 0 && (
          <div className="news-list">
            {sick.map((s) => (
              <SourceRow key={s.host} s={s} />
            ))}
            {healthy.map((s) => (
              <SourceRow key={s.host} s={s} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
