"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchVolOverlay, fetchFactors } from "@/lib/api";
import type { VolOverlayStratMetrics } from "@market/shared";

function pct(n: number | undefined, digits = 0): string {
  return n == null ? "—" : `${(n * 100).toFixed(digits)}%`;
}
function f2(n: number | undefined): string {
  return n == null ? "—" : n.toFixed(2);
}

// Exposure: full size in calm vol (green), trimming as forecast vol rises (red).
function expColor(e: number | undefined): string {
  if (e == null) return "var(--text-dim)";
  if (e >= 0.9) return "var(--green)";
  if (e >= 0.7) return "var(--yellow)";
  return "var(--red)";
}

function Stat({ label, value, color, sub }: {
  label: string; value: string; color?: string; sub?: string;
}) {
  return (
    <div style={{ minWidth: 92 }}>
      <div style={{ fontSize: 9, color: "var(--text-dim)" }}>{label}</div>
      <div className="num" style={{ fontSize: 17, color: color ?? "var(--text)" }}>{value}</div>
      {sub && <div style={{ fontSize: 9, color: "var(--text-dim)" }}>{sub}</div>}
    </div>
  );
}

function MetricRow({ name, m, recommended }: {
  name: string; m: VolOverlayStratMetrics; recommended?: boolean;
}) {
  return (
    <tr className="wl-row" style={recommended ? { fontWeight: 600 } : { color: "var(--text-dim)" }}>
      <td className="wl-sym">{name}{recommended ? " ★" : ""}</td>
      <td className="num">{f2(m.sharpe)}</td>
      <td className="num" style={{ color: "var(--red)" }}>{pct(m.maxdd)}</td>
      <td className="num">{pct(m.cagr, 1)}</td>
      <td className="num">{f2(m.calmar)}</td>
    </tr>
  );
}

export function VolOverlayPanel() {
  const q = useQuery({
    queryKey: ["vol-overlay"],
    queryFn: () => fetchVolOverlay("SPY", 0.15),
    refetchInterval: 30 * 60_000,
  });

  const fq = useQuery({
    queryKey: ["factors"],
    queryFn: () => fetchFactors(),
    refetchInterval: 60 * 60_000,
  });

  const sig = q.data?.signal;
  const bt = q.data?.backtest;
  const bh = bt?.buy_hold;
  const vt = bt?.vol_target;
  const dSharpe = vt?.sharpe != null && bh?.sharpe != null ? vt.sharpe - bh.sharpe : undefined;
  const leaders = fq.data?.leaders ?? [];
  const laggards = fq.data?.laggards ?? [];

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Quant Overlay (§12) — vol-target + factor screen</span>
        <span style={{ fontSize: 9, color: "var(--text-dim)" }}>
          {sig ? `as of ${sig.as_of}` : ""}
        </span>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && !sig && (
          <div style={{ color: "var(--text-dim)" }}>loading vol-overlay…</div>
        )}

        {sig && (
          <>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginBottom: 10 }}>
              <Stat
                label="Suggested exposure"
                value={pct(sig.suggested_exposure)}
                color={expColor(sig.suggested_exposure)}
                sub={sig.regime === "stress" ? "stress regime" : "of full SPY"}
              />
              <Stat
                label="Forecast vol (ann)"
                value={pct(sig.forecast_vol_annual, 1)}
                color={sig.vol_percentile >= 0.8 ? "var(--red)" : "var(--text)"}
                sub={`${pct(sig.vol_percentile)}-ile · target ${pct(sig.target_vol)}`}
              />
            </div>

            {bh && vt && (
              <>
                <div className="macro-detail-title">
                  Backtest since 1993 (net 1bp, de-risk only)
                </div>
                <table className="wl-table" style={{ width: "100%", fontSize: 11 }}>
                  <thead>
                    <tr style={{ color: "var(--text-dim)", fontSize: 9 }}>
                      <th style={{ textAlign: "left" }}>strategy</th>
                      <th style={{ textAlign: "right" }}>Sharpe</th>
                      <th style={{ textAlign: "right" }}>MaxDD</th>
                      <th style={{ textAlign: "right" }}>CAGR</th>
                      <th style={{ textAlign: "right" }}>Calmar</th>
                    </tr>
                  </thead>
                  <tbody>
                    <MetricRow name="vol-target" m={vt} recommended />
                    <MetricRow name="buy & hold" m={bh} />
                  </tbody>
                </table>
                {dSharpe != null && (
                  <div style={{ fontSize: 10, marginTop: 6, color: "var(--green)" }}>
                    +{dSharpe.toFixed(2)} Sharpe vs buy&amp;hold, drawdown{" "}
                    {bh.maxdd != null && vt.maxdd != null
                      ? `${pct(bh.maxdd)} → ${pct(vt.maxdd)}`
                      : ""}
                  </div>
                )}
              </>
            )}

            {(leaders.length > 0 || laggards.length > 0) && (
              <div style={{ marginTop: 12 }}>
                <div className="macro-detail-title">
                  Fundamental factor screen (value/quality/small)
                  {fq.data?.as_of ? ` · ${fq.data.as_of}` : ""}
                </div>
                <div style={{ display: "flex", gap: 18, fontSize: 11 }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 9, color: "var(--green)" }}>cheap/quality ▲</div>
                    {leaders.map((r) => (
                      <div key={r.symbol} className="wl-row" style={{ display: "flex", justifyContent: "space-between" }}>
                        <span className="wl-sym">{r.symbol}</span>
                        <span className="num" style={{ color: "var(--green)" }}>{r.score.toFixed(2)}</span>
                      </div>
                    ))}
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 9, color: "var(--red)" }}>expensive/growth ▼</div>
                    {laggards.map((r) => (
                      <div key={r.symbol} className="wl-row" style={{ display: "flex", justifyContent: "space-between" }}>
                        <span className="wl-sym">{r.symbol}</span>
                        <span className="num" style={{ color: "var(--red)" }}>{r.score.toFixed(2)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 10 }}>
              Vol-target sizes SPY inversely to the Garman-Klass vol forecast — the one signal that
              is genuinely forecastable. Defensive risk control, NOT alpha. Factor screen ranks names
              on EDGAR-XBRL value/quality/size (descriptive — composite fails DSR, so it nudges the
              Strategist&apos;s picks, never drives them). Both feed the Strategist.
            </div>
          </>
        )}
      </div>
    </div>
  );
}
