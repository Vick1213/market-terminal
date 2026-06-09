"use client";

import { useQuery } from "@tanstack/react-query";
import type { HeartbeatMessage } from "@market/shared";
import { fetchHealth } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";

export function HeartbeatPanel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 5000,
  });

  const { status, messages } = useWebSocket("heartbeat");
  const ticks = messages.filter(
    (m): m is HeartbeatMessage => (m as HeartbeatMessage).type === "heartbeat",
  );
  const last = ticks[0];

  return (
    <div className="panel">
      <div className="panel-head">
        <span>System · Phase 0</span>
        <span className={`badge ${status === "open" ? "live" : "closed"}`}>
          {status === "open" ? "WS live" : status}
        </span>
      </div>
      <div className="panel-body">
        {isError && <div style={{ color: "var(--red)" }}>API unreachable — is the backend running on :8000?</div>}
        {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}
        {data && (
          <div className="kv">
            <span className="k">REST</span>
            <span className="v" style={{ color: "var(--green)" }}>
              {data.status}
            </span>
            <span className="k">App</span>
            <span className="v">
              {data.app} v{data.version}
            </span>
            <span className="k">Scheduler</span>
            <span className="v">{data.scheduler_running ? "running" : "stopped"}</span>
            <span className="k">Watchlist</span>
            <span className="v">{data.watchlist_count} symbols</span>
            <span className="k">DuckDB tables</span>
            <span className="v">{Object.keys(data.duckdb_tables).length}</span>
            <span className="k">WS clients</span>
            <span className="v">{data.ws_clients}</span>
            <span className="k">Last tick</span>
            <span className="v">{last ? `#${last.n}` : "—"}</span>
          </div>
        )}

        <ul className="tick-list">
          {ticks.slice(0, 6).map((t) => (
            <li key={t.n}>
              #{t.n} · {new Date(t.ts).toLocaleTimeString()} · {t.ws_clients} client
              {t.ws_clients === 1 ? "" : "s"}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
