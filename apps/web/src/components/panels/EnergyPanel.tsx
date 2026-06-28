"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchEiaEnergy, runEiaEnergy } from "@/lib/api";

function num(n: number | undefined, digits = 2): string {
  return n == null ? "—" : n.toFixed(digits);
}

// A build (positive inventory change) is bearish for crude; a draw (negative)
// is bullish. For nat-gas storage the same sign convention applies.
function changeColor(chg: number | undefined, bullishWhenNegative = true): string {
  if (chg == null) return "var(--text-dim)";
  const bullish = bullishWhenNegative ? chg < 0 : chg > 0;
  return bullish ? "var(--green)" : "var(--red)";
}

function Stat({
  label,
  value,
  color,
  sub,
}: {
  label: string;
  value: string;
  color?: string;
  sub?: string;
}) {
  return (
    <div style={{ minWidth: 88 }}>
      <div style={{ fontSize: 9, color: "var(--text-dim)" }}>{label}</div>
      <div className="num" style={{ fontSize: 14, color: color ?? "var(--text)" }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 9, color: "var(--text-dim)" }}>{sub}</div>}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ flex: 1, minWidth: 200 }}>
      <div className="macro-detail-title">{title}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>{children}</div>
    </div>
  );
}

export function EnergyPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["eia-energy"],
    queryFn: () => fetchEiaEnergy(52),
    refetchInterval: 30 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: runEiaEnergy,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["eia-energy"] }),
  });

  const stocks = q.data?.stocks ?? [];
  const spot = q.data?.spot ?? [];
  const natgas = q.data?.natgas ?? [];
  const s = stocks[stocks.length - 1];
  const p = spot[spot.length - 1];
  const g = natgas[natgas.length - 1];
  const hasData = !!(s || p || g);

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Energy Fundamentals — EIA Weekly</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          style={{ fontSize: 10 }}
          title="Pull the latest EIA weekly petroleum + nat-gas report now"
        >
          {refresh.isPending ? "…" : "refresh"}
        </button>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && !hasData && (
          <div style={{ color: "var(--text-dim)" }}>no energy data — EIA reports weekly</div>
        )}

        {hasData && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 24 }}>
            {p && (
              <Section title={`Spot prices${p.date ? ` · ${p.date}` : ""}`}>
                <Stat label="WTI $/bbl" value={num(p.wti)} />
                <Stat label="Brent $/bbl" value={num(p.brent)} />
                <Stat label="RBOB $/gal" value={num(p.rbob)} />
                <Stat
                  label="3:2:1 crack"
                  value={num(p.crack_321)}
                  color={
                    p.crack_321 == null
                      ? undefined
                      : p.crack_321 >= 25
                        ? "var(--green)"
                        : p.crack_321 < 12
                          ? "var(--red)"
                          : "var(--yellow)"
                  }
                  sub="$/bbl"
                />
              </Section>
            )}
            {s && (
              <Section title={`Inventories${s.date ? ` · ${s.date}` : ""}`}>
                <Stat
                  label="Crude (commercial)"
                  value={num(s.crude_commercial, 1)}
                  sub="mmbbl"
                />
                <Stat
                  label="Crude wow"
                  value={
                    s.crude_commercial_chg == null
                      ? "—"
                      : `${s.crude_commercial_chg >= 0 ? "+" : ""}${s.crude_commercial_chg.toFixed(1)}`
                  }
                  color={changeColor(s.crude_commercial_chg)}
                  sub={s.crude_commercial_chg == null ? "" : s.crude_commercial_chg < 0 ? "draw" : "build"}
                />
                <Stat label="Gasoline" value={num(s.gasoline, 1)} sub="mmbbl" />
                <Stat label="Distillate" value={num(s.distillate, 1)} sub="mmbbl" />
              </Section>
            )}
            {g && (
              <Section title={`Nat-gas storage${g.date ? ` · ${g.date}` : ""}`}>
                <Stat label="Working gas" value={num(g.storage, 0)} sub="Bcf" />
                <Stat
                  label="Weekly chg"
                  value={g.change == null ? "—" : `${g.change >= 0 ? "+" : ""}${g.change.toFixed(0)}`}
                  color={changeColor(g.change)}
                  sub="Bcf"
                />
                <Stat
                  label="vs 5-yr"
                  value={g.vs_5yr_pct == null ? "—" : `${g.vs_5yr_pct >= 0 ? "+" : ""}${g.vs_5yr_pct.toFixed(1)}%`}
                  color={changeColor(g.vs_5yr_pct, false)}
                />
              </Section>
            )}
          </div>
        )}

        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 10 }}>
          Crude draw (green) = bullish, build (red) = bearish. 3:2:1 crack = refinery margin
          (2·RBOB + heating-oil − 3·WTI, ×42 to $/bbl). Nat-gas surplus vs the 5-yr average (green) is
          bearish for gas.
        </div>
      </div>
    </div>
  );
}
