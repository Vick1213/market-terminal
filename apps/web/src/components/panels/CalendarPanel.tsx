"use client";

import { useQuery } from "@tanstack/react-query";
import type { CalendarEvent } from "@market/shared";
import { fetchCalendar } from "@/lib/api";

const KIND_STYLE: Record<string, { icon: string; color: string }> = {
  fomc: { icon: "🏛", color: "var(--red)" },
  cpi: { icon: "📊", color: "var(--yellow)" },
  nfp: { icon: "👷", color: "var(--yellow)" },
  ppi: { icon: "🏭", color: "var(--text-dim)" },
  gdp: { icon: "🌐", color: "var(--text-dim)" },
  pce: { icon: "🛒", color: "var(--yellow)" },
  opex: { icon: "⏳", color: "var(--blue, #4ea1ff)" },
  cot: { icon: "⚖", color: "var(--text-dim)" },
  earnings: { icon: "🧾", color: "var(--green)" },
};

function countdown(days: number): string {
  if (days === 0) return "TODAY";
  if (days === 1) return "tomorrow";
  return `in ${days}d`;
}

function EventRow({ e }: { e: CalendarEvent }) {
  const style = KIND_STYLE[e.kind] ?? { icon: "•", color: "var(--text-dim)" };
  const hot = e.days_until <= 1 && ["fomc", "cpi", "nfp", "pce"].includes(e.kind);
  return (
    <tr className="wl-row" title={`${e.date} · source: ${e.source}`}>
      <td
        className="num"
        style={{
          color: hot ? "var(--red)" : e.days_until <= 3 ? "var(--yellow)" : "var(--text-dim)",
          fontWeight: hot ? 700 : undefined,
          whiteSpace: "nowrap",
        }}
      >
        {countdown(e.days_until)}
      </td>
      <td style={{ whiteSpace: "nowrap" }}>
        <span style={{ marginRight: 6 }}>{style.icon}</span>
        <span style={{ color: style.color }}>{e.title}</span>
        {e.source === "estimated" && (
          <span style={{ color: "var(--text-dim)", fontSize: 10, marginLeft: 4 }} title="date estimated (first Friday); set MARKET_FRED_API_KEY for official schedule">
            ~est
          </span>
        )}
      </td>
      <td className="num" style={{ color: "var(--text-dim)" }}>
        {e.date.slice(5)}
      </td>
    </tr>
  );
}

export function CalendarPanel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["calendar"],
    queryFn: () => fetchCalendar(45),
    refetchInterval: 60 * 60_000,
  });

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Event Horizon</span>
        <span style={{ fontSize: 10, color: "var(--text-dim)" }} title="FOMC static schedule · CPI/NFP via FRED release calendar · OPEX computed · earnings via Yahoo">
          next 45d
        </span>
      </div>
      <div className="panel-body">
        {isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}
        {data && data.events.length === 0 && (
          <div style={{ color: "var(--text-dim)" }}>
            warming up — the calendar populates within ~7 min of boot
          </div>
        )}
        {data && data.events.length > 0 && (
          <table className="wl-table">
            <tbody>
              {data.events.map((e) => (
                <EventRow key={e.id} e={e} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
