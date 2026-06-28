"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { PolymarketMarket, WeekendGap } from "@market/shared";
import { fetchDivergence, fetchPolymarket, runDivergence, runPolymarket } from "@/lib/api";

function scoreColor(score: number): string {
  if (score >= 75) return "var(--red)";
  if (score >= 55) return "var(--yellow)";
  return "var(--text-dim)";
}

function toneLabel(t: number | null): { text: string; color: string } {
  if (t === null) return { text: "n/a", color: "var(--text-dim)" };
  if (t > 0.1) return { text: `calm ${t.toFixed(2)}`, color: "var(--green)" };
  if (t < -0.1) return { text: `fearful ${t.toFixed(2)}`, color: "var(--red)" };
  return { text: `mixed ${t.toFixed(2)}`, color: "var(--text-dim)" };
}

function MarketRow({ m }: { m: PolymarketMarket }) {
  const move = m.move ?? 0;
  const towardDanger = move * (m.escalation_sign || 0) > 0;
  const moveColor = !m.move
    ? "var(--text-dim)"
    : towardDanger
      ? "var(--red)"
      : "var(--green)";
  return (
    <tr className="wl-row" title={`${m.question}\nends ${m.end_date ?? "?"} · vol ${m.volume ? `$${(m.volume / 1e6).toFixed(1)}m` : "?"}`}>
      <td style={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {m.url ? (
          <a href={m.url} target="_blank" rel="noreferrer" style={{ color: "inherit" }}>
            {m.question}
          </a>
        ) : (
          m.question
        )}
        {m.escalation_sign !== 0 && (
          <span style={{ color: "var(--text-dim)", marginLeft: 4 }}>
            {m.escalation_sign > 0 ? "↑war" : "↓calm"}
          </span>
        )}
      </td>
      <td className="num">{m.yes_prob !== null ? `${(m.yes_prob * 100).toFixed(0)}%` : "—"}</td>
      <td className="num" style={{ color: moveColor }}>
        {m.move !== null ? `${move >= 0 ? "+" : ""}${(move * 100).toFixed(0)}pp` : "—"}
      </td>
      <td
        className="num"
        style={{ color: m.top_share && m.top_share >= 0.25 ? "var(--yellow)" : "var(--text-dim)" }}
        title="largest holder share of the tracked side"
      >
        {m.top_share !== null ? `${(m.top_share * 100).toFixed(0)}%` : "—"}
      </td>
    </tr>
  );
}

function GapRow({ g }: { g: WeekendGap }) {
  return (
    <span
      title={`${g.from} close ${g.prev_close} → ${g.to} open ${g.next_open}`}
      style={{
        fontSize: 10,
        padding: "1px 5px",
        borderRadius: 3,
        background: "var(--bg-2, #1a1a1a)",
        color: g.gap_pct >= 0 ? "var(--green)" : "var(--red)",
        whiteSpace: "nowrap",
      }}
    >
      {g.to.slice(5)} {g.gap_pct >= 0 ? "+" : ""}
      {g.gap_pct}%
    </span>
  );
}

export function DivergencePanel() {
  const qc = useQueryClient();
  const div = useQuery({
    queryKey: ["divergence"],
    queryFn: fetchDivergence,
    refetchInterval: 5 * 60_000,
  });
  const pm = useQuery({
    queryKey: ["polymarket"],
    queryFn: fetchPolymarket,
    refetchInterval: 10 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: async () => {
      await runPolymarket();
      return runDivergence();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["divergence"] });
      qc.invalidateQueries({ queryKey: ["polymarket"] });
    },
  });

  const latest = div.data?.latest;
  const sig = latest?.detail?.signals;
  const score = latest?.score ?? null;
  const markets = pm.data?.markets ?? latest?.detail?.polymarket?.markets ?? [];
  const gaps = div.data?.weekend_gaps ?? [];
  const tone = toneLabel(latest?.narrative ?? null);

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Divergence — Narrative vs Money</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          style={{ fontSize: 10 }}
          title="Re-snapshot Polymarket odds and recompute the divergence score now"
        >
          {refresh.isPending ? "…" : "refresh"}
        </button>
      </div>
      <div className="panel-body">
        {div.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!div.isError && !latest && (
          <div style={{ color: "var(--text-dim)" }}>
            warming up — the engine runs ~12 min after boot (needs price + news + Polymarket data)
          </div>
        )}

        {latest && (
          <>
            <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 4 }}>
              <span
                style={{ fontSize: 30, fontWeight: 700, color: scoreColor(score ?? 0), lineHeight: 1 }}
                title="0–100: high = money/markets bracing while the official narrative reads calm"
              >
                {score !== null ? score.toFixed(0) : "—"}
              </span>
              <span style={{ fontSize: 10, color: "var(--text-dim)" }}>
                / 100 divergence{latest.regime ? ` · regime ${latest.regime}` : ""}
              </span>
            </div>
            <div style={{ fontSize: 11, marginBottom: 8 }}>{latest.headline}</div>

            {/* The three legs */}
            <div style={{ display: "flex", gap: 12, fontSize: 11, marginBottom: 8, flexWrap: "wrap" }}>
              <span title="cross-asset risk-off basket z (defense/oil/gold/bonds/$/VIX up, equities down)">
                money:{" "}
                <b style={{ color: (latest.riskoff_z ?? 0) > 0 ? "var(--red)" : "var(--green)" }}>
                  {latest.riskoff_z !== null ? `${latest.riskoff_z >= 0 ? "+" : ""}${latest.riskoff_z.toFixed(1)}σ` : "n/a"}
                </b>
              </span>
              <span title="FinBERT tone of recent geopolitical headlines (+ = calm/reassuring)">
                news: <b style={{ color: tone.color }}>{tone.text}</b>
                {latest.detail?.narrative?.n_headlines ? (
                  <span style={{ color: "var(--text-dim)" }}> ({latest.detail.narrative.n_headlines})</span>
                ) : null}
              </span>
              <span title="Polymarket geopolitical odds drift toward escalation (volume-weighted)">
                polymarket:{" "}
                <b style={{ color: (latest.pm_pressure ?? 0) > 0 ? "var(--red)" : "var(--green)" }}>
                  {latest.pm_pressure !== null ? latest.pm_pressure.toFixed(2) : "n/a"}
                </b>
              </span>
            </div>

            {sig && sig.money >= 0.5 && sig.pm >= 0.4 && sig.calm >= 0.3 && (
              <div style={{ color: "var(--red)", fontSize: 11, marginBottom: 8 }}>
                ⚠ Cross-venue confirmation — equity hedging, Polymarket escalation and calm news all agree.
              </div>
            )}

            {/* Risk-off legs */}
            {!!latest.detail?.riskoff_legs?.length && (
              <>
                <div className="macro-detail-title">risk-off basket (2-day move, signed z)</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, fontSize: 11, marginBottom: 8 }}>
                  {latest.detail.riskoff_legs.map((l) => (
                    <span key={l.symbol} title={`${l.symbol}: ${l.ret_2d} over 2d, z ${l.z}, signed ${l.signed}`}>
                      <span className="wl-sym">{l.symbol}</span>{" "}
                      <span style={{ color: l.signed > 0 ? "var(--red)" : "var(--green)" }}>
                        {l.signed >= 0 ? "+" : ""}
                        {l.signed}
                      </span>
                    </span>
                  ))}
                </div>
              </>
            )}
          </>
        )}

        {/* Polymarket markets */}
        {!!markets.length && (
          <>
            <div className="macro-detail-title" style={{ marginTop: 6 }}>
              Polymarket geopolitical markets (Δ vs ~24h)
            </div>
            <table className="wl-table">
              <thead>
                <tr>
                  <th>market</th>
                  <th className="num">yes</th>
                  <th className="num">move</th>
                  <th className="num" title="top holder concentration">conc.</th>
                </tr>
              </thead>
              <tbody>
                {markets.slice(0, 8).map((m) => (
                  <MarketRow key={m.id} m={m} />
                ))}
              </tbody>
            </table>
          </>
        )}

        {/* Forward-outcome track record */}
        {!!gaps.length && (
          <>
            <div className="macro-detail-title" style={{ marginTop: 8 }} title="realised SPY gap across each non-trading break — the weekend-news outcome the score is meant to anticipate">
              SPY weekend gaps (outcome)
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {gaps.map((g) => (
                <GapRow key={`${g.from}:${g.to}`} g={g} />
              ))}
            </div>
          </>
        )}

        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 8 }}>
          Heuristic signal, not proof of insider activity. Polymarket wallets are pseudonymous.
        </div>
      </div>
    </div>
  );
}
