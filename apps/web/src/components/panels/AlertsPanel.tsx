"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { AlertItem, AlertSeverity, AlertWsMessage } from "@market/shared";
import { fetchAlerts, testNtfy } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";

function sevColor(s: AlertSeverity): string {
  if (s === "critical") return "var(--red)";
  if (s === "warn") return "var(--yellow)";
  return "var(--text-dim)";
}

const RULE_ICONS: Record<string, string> = {
  regime_flip: "◑",
  macro_z: "σ",
  vix_term: "〽",
  corr_break: "⛓",
  retail_spike: "📈",
  insider_cluster: "👤",
  cot_extreme: "⚖",
  gex_flip: "Γ",
  large_print: "🐋",
};

function AlertRow({ a }: { a: AlertItem }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      className="news-item"
      onClick={() => setOpen((v) => !v)}
      style={{ cursor: "pointer" }}
      title={open ? "" : a.body ?? ""}
    >
      <div className="news-title">
        <span style={{ color: sevColor(a.severity), marginRight: 6 }}>
          {RULE_ICONS[a.rule] ?? "•"}
        </span>
        {a.title}
      </div>
      <div className="news-meta">
        <span style={{ color: sevColor(a.severity) }}>{a.severity}</span>
        <span>{a.rule.replace(/_/g, " ")}</span>
        {a.regime && <span>regime: {a.regime}</span>}
        {a.pushed && <span title="delivered to ntfy">📲</span>}
        <span>
          {new Date(a.ts + "Z").toLocaleString([], {
            month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
          })}
        </span>
      </div>
      {open && a.body && (
        <div style={{ fontSize: 11, color: "var(--text-dim)", marginTop: 4 }}>{a.body}</div>
      )}
    </div>
  );
}

export function AlertsPanel() {
  const queryClient = useQueryClient();
  const [testState, setTestState] = useState<string | null>(null);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["alerts"],
    queryFn: () => fetchAlerts(50),
    refetchInterval: 5 * 60_000, // WS pushes trigger earlier refreshes
  });

  const { status, last } = useWebSocket("alerts", 10);
  useEffect(() => {
    if ((last as AlertWsMessage | undefined)?.type === "alert") {
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
    }
  }, [last, queryClient]);

  const onTest = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setTestState("…");
    try {
      const r = await testNtfy();
      setTestState(r.ok ? "sent ✓" : r.detail ?? "failed");
    } catch {
      setTestState("failed");
    }
    setTimeout(() => setTestState(null), 4000);
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Alerts</span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {data && (
            <span
              className="filing-chip"
              style={{ color: data.ntfy_enabled ? "var(--green)" : "var(--text-dim)" }}
              title={
                data.ntfy_enabled
                  ? "warn/critical alerts push to your ntfy topic"
                  : "set MARKET_NTFY_TOPIC for phone push — alerts stay in-app"
              }
            >
              {data.ntfy_enabled ? "● ntfy" : "○ ntfy off"}
            </span>
          )}
          {data?.ntfy_enabled && (
            <button className="expand-btn" onClick={onTest} title="Send a test push">
              {testState ?? "test"}
            </button>
          )}
          <span className={`badge ${status === "open" ? "live" : "closed"}`}>
            {status === "open" ? "WS live" : status}
          </span>
        </span>
      </div>
      <div className="panel-body">
        {isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}
        {data && data.alerts.length === 0 && (
          <div style={{ color: "var(--text-dim)" }}>
            no alerts yet — the sweep runs every 10 min over regime flips, z-spikes,
            correlation breaks, retail spikes, insider clusters, COT extremes and the
            gamma flip
          </div>
        )}
        {data && data.alerts.length > 0 && (
          <div className="news-list">
            {data.alerts.map((a) => (
              <AlertRow key={a.id} a={a} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
