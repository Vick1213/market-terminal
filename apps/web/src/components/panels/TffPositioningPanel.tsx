"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { TffMarket } from "@market/shared";
import { fetchCftcTff, runCftcTff } from "@/lib/api";

// Compact a net-position contract count to a readable ±k/±M figure.
function fmtNet(n: number | null): string {
  if (n == null) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "−" : "+";
  if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(0)}k`;
  return `${sign}${abs.toFixed(0)}`;
}

// The divergence index is a 0–100 percentile; 50 is neutral, extremes are the
// stretched real-money-vs-hedge-fund setups.
function idxColor(idx: number | null): string {
  if (idx == null) return "var(--text-dim)";
  const d = Math.abs(idx - 50);
  if (d >= 40) return "var(--red)";
  if (d >= 25) return "var(--yellow)";
  return "var(--text-dim)";
}

function MarketRow({ m }: { m: TffMarket }) {
  const widening =
    m.div_13w_ago != null ? Math.abs(m.am_lm_div) > Math.abs(m.div_13w_ago) : null;
  const title = [
    `${m.ticker} — report ${m.report_date} (${m.weeks}w history)`,
    `Asset-Mgr net ${fmtNet(m.am_net)} · Lev-Money net ${fmtNet(m.lm_net)} · Dealer ${fmtNet(m.dealer_net)}`,
    `AM−LM divergence ${fmtNet(m.am_lm_div)} (13w ago ${fmtNet(m.div_13w_ago)})`,
    m.div_index != null ? `3y percentile ${m.div_index}` : null,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <tr className="wl-row" title={title}>
      <td>
        <span className="wl-sym">{m.ticker}</span>
      </td>
      <td className="num" style={{ color: m.am_net >= 0 ? "var(--green)" : "var(--red)" }}>
        {fmtNet(m.am_net)}
      </td>
      <td className="num" style={{ color: m.lm_net >= 0 ? "var(--green)" : "var(--red)" }}>
        {fmtNet(m.lm_net)}
      </td>
      <td className="num">{fmtNet(m.am_lm_div)}</td>
      <td className="num" style={{ color: idxColor(m.div_index) }}>
        {m.div_index == null ? "—" : m.div_index.toFixed(0)}
      </td>
      <td className="num" style={{ color: "var(--text-dim)" }}>
        {widening == null ? "" : widening ? "↑" : "↓"}
      </td>
    </tr>
  );
}

export function TffPositioningPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["cftc-tff"],
    queryFn: fetchCftcTff,
    refetchInterval: 30 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: runCftcTff,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cftc-tff"] }),
  });

  const markets = q.data?.markets ?? [];

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Positioning — Real Money vs Hedge Funds (TFF)</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          style={{ fontSize: 10 }}
          title="Pull the latest CFTC Traders-in-Financial-Futures report now"
        >
          {refresh.isPending ? "…" : "refresh"}
        </button>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && !markets.length && (
          <div style={{ color: "var(--text-dim)" }}>no positioning data — CFTC reports weekly</div>
        )}

        {!!markets.length && (
          <table className="wl-table">
            <thead>
              <tr>
                <th>contract</th>
                <th className="num">AM net</th>
                <th className="num">LM net</th>
                <th className="num">div</th>
                <th className="num">%ile</th>
                <th className="num">13w</th>
              </tr>
            </thead>
            <tbody>
              {markets.map((m) => (
                <MarketRow key={m.ticker} m={m} />
              ))}
            </tbody>
          </table>
        )}

        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 8 }}>
          AM = asset managers (real money), LM = leveraged money (hedge funds). div = AM−LM net — the
          real-money-vs-HF split the legacy commercial/non-commercial COT hides. %ile is the 3-year
          divergence percentile (red = stretched); 13w arrow = widening (↑) or narrowing (↓) vs 13
          weeks ago. Sorted most-stretched first.
        </div>
      </div>
    </div>
  );
}
