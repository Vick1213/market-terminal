"use client";

import { useQuery } from "@tanstack/react-query";
import type { CotMarket, GexSnapshot, ShortInterestItem } from "@market/shared";
import { fetchCot, fetchGex, fetchInsider, fetchShortInterest } from "@/lib/api";

function cotColor(idx: number | null): string {
  if (idx === null) return "var(--text-dim)";
  if (idx >= 90 || idx <= 10) return "var(--red)";
  if (idx >= 75 || idx <= 25) return "var(--yellow)";
  return "var(--text-dim)";
}

/** 0..100 percentile bar with the extreme deciles tinted. */
function CotBar({ idx }: { idx: number | null }) {
  if (idx === null) return <span style={{ color: "var(--text-dim)" }}>—</span>;
  return (
    <div className="gauge-track" style={{ height: 6 }} title={`COT index ${idx} (3y percentile of net spec positioning)`}>
      <div
        className="gauge-fill"
        style={{ left: 0, width: `${idx}%`, background: cotColor(idx) }}
      />
    </div>
  );
}

function CotTable({ markets }: { markets: CotMarket[] }) {
  return (
    <table className="wl-table">
      <thead>
        <tr>
          <th>futures</th>
          <th className="num">net spec</th>
          <th className="num">13w Δ</th>
          <th style={{ width: "30%" }}>COT idx (3y)</th>
        </tr>
      </thead>
      <tbody>
        {markets.map((m) => {
          const delta = m.net_13w_ago !== null ? m.net_noncommercial - m.net_13w_ago : null;
          return (
            <tr key={m.market_code} className="wl-row" title={`as of ${m.report_date} (${m.weeks} weekly prints)`}>
              <td className="wl-sym">{m.market}</td>
              <td className="num" style={{ color: m.net_noncommercial >= 0 ? "var(--green)" : "var(--red)" }}>
                {(m.net_noncommercial / 1000).toFixed(0)}k
              </td>
              <td className="num" style={{ color: delta === null ? undefined : delta >= 0 ? "var(--green)" : "var(--red)" }}>
                {delta === null ? "—" : `${delta >= 0 ? "+" : ""}${(delta / 1000).toFixed(0)}k`}
              </td>
              <td>
                <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <CotBar idx={m.cot_index} />
                  <span className="num" style={{ color: cotColor(m.cot_index), fontSize: 10, minWidth: 22 }}>
                    {m.cot_index ?? "—"}
                  </span>
                </span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function GexStrip({ snap }: { snap: GexSnapshot }) {
  const neg = snap.total_gex_bn !== null && snap.total_gex_bn < 0;
  const belowFlip = snap.spot !== null && snap.flip !== null && snap.spot < snap.flip;
  return (
    <div
      className="regime-banner"
      style={{ borderColor: neg || belowFlip ? "var(--red)" : "var(--green)", marginTop: 8 }}
      title={`self-computed from the CBOE 15-min-delayed ${snap.symbol} chain (${snap.contracts ?? "?"} contracts, expiries ≤45d) — as of ${snap.ts}Z`}
    >
      <span className="regime-tag" style={{ color: neg || belowFlip ? "var(--red)" : "var(--green)" }}>
        {snap.symbol.replace("_", "")} Γ {neg ? "SHORT" : "LONG"}
      </span>
      <span style={{ fontSize: 11, color: "var(--text-dim)" }}>
        {snap.total_gex_bn !== null && `GEX ${snap.total_gex_bn > 0 ? "+" : ""}${snap.total_gex_bn}bn/1%`}
        {snap.flip !== null && ` · flip ${snap.flip.toLocaleString()}`}
        {snap.call_wall !== null && ` · call wall ${snap.call_wall.toLocaleString()}`}
        {snap.put_wall !== null && ` · put wall ${snap.put_wall.toLocaleString()}`}
        {snap.spot !== null && ` · spot ${Math.round(snap.spot).toLocaleString()}`}
      </span>
    </div>
  );
}

function dtcColor(s: ShortInterestItem): string {
  if (s.days_to_cover !== null && s.days_to_cover >= 5) return "var(--red)";
  if ((s.dtc_percentile ?? 0) >= 90 || (s.change_pct ?? 0) >= 25) return "var(--yellow)";
  return "var(--text-dim)";
}

function ShortInterestTable({ symbols }: { symbols: ShortInterestItem[] }) {
  return (
    <table className="wl-table">
      <thead>
        <tr>
          <th>symbol</th>
          <th className="num">shares short</th>
          <th className="num">Δ print</th>
          <th className="num">days to cover</th>
        </tr>
      </thead>
      <tbody>
        {symbols.map((s) => (
          <tr
            key={s.symbol}
            className="wl-row"
            title={`FINRA settlement ${s.settlement_date} (${s.prints} bi-monthly prints stored) · days-to-cover percentile ${s.dtc_percentile ?? "—"} vs own history`}
          >
            <td className="wl-sym">{s.symbol}</td>
            <td className="num">
              {s.shares_short !== null ? `${(s.shares_short / 1e6).toFixed(1)}M` : "—"}
            </td>
            <td
              className="num"
              style={{
                color:
                  s.change_pct === null
                    ? undefined
                    : s.change_pct > 0
                      ? "var(--red)"
                      : "var(--green)",
              }}
            >
              {s.change_pct !== null
                ? `${s.change_pct >= 0 ? "+" : ""}${s.change_pct.toFixed(1)}%`
                : "—"}
            </td>
            <td className="num" style={{ color: dtcColor(s) }}>
              {s.days_to_cover !== null ? s.days_to_cover.toFixed(2) : "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function PositioningPanel() {
  const cot = useQuery({ queryKey: ["cot"], queryFn: fetchCot, refetchInterval: 60 * 60_000 });
  const gex = useQuery({ queryKey: ["gex"], queryFn: fetchGex, refetchInterval: 15 * 60_000 });
  const insider = useQuery({
    queryKey: ["insider"],
    queryFn: () => fetchInsider(30),
    refetchInterval: 60 * 60_000,
  });
  const shortInterest = useQuery({
    queryKey: ["shortInterest"],
    queryFn: fetchShortInterest,
    refetchInterval: 6 * 60 * 60_000, // bi-monthly prints — slow poll
  });

  const anyError = cot.isError && gex.isError && insider.isError;
  const empty =
    !cot.data?.markets.length &&
    !gex.data?.snapshots.length &&
    !insider.data?.clusters.length;

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Smart Money — COT · Gamma · Insiders</span>
        <span style={{ fontSize: 10, color: "var(--text-dim)" }} title="COT weekly (Fri) · GEX 15-min delayed · Form 4 same-day">
          lagged sources
        </span>
      </div>
      <div className="panel-body">
        {anyError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!anyError && empty && (
          <div style={{ color: "var(--text-dim)" }}>
            warming up — COT/GEX/Form 4 land within ~10 min of boot
          </div>
        )}

        {gex.data?.snapshots.map((s) => <GexStrip key={s.symbol} snap={s} />)}

        {!!cot.data?.markets.length && (
          <>
            <div className="macro-detail-title" style={{ marginTop: 10 }}>
              CFTC positioning (non-commercial)
            </div>
            <CotTable markets={cot.data.markets} />
          </>
        )}

        {!!shortInterest.data?.symbols.length && (
          <>
            <div className="macro-detail-title" style={{ marginTop: 10 }}>
              True short interest — FINRA bi-monthly (≠ daily short volume)
            </div>
            <ShortInterestTable symbols={shortInterest.data.symbols} />
          </>
        )}

        {!!insider.data?.clusters.length && (
          <>
            <div className="macro-detail-title" style={{ marginTop: 10 }}>
              Form 4 open-market buys — watchlist, 30d
            </div>
            <table className="wl-table">
              <thead>
                <tr>
                  <th>symbol</th>
                  <th className="num">buyers</th>
                  <th className="num">total</th>
                  <th className="num">last filed</th>
                </tr>
              </thead>
              <tbody>
                {insider.data.clusters.map((c) => (
                  <tr
                    key={c.symbol}
                    className="wl-row"
                    title={
                      (c.buyers >= 2 ? "CLUSTER: multiple insiders buying. " : "") +
                      (c.has_ceo_cfo ? "Includes a CEO/CFO buy." : "")
                    }
                  >
                    <td>
                      <span className="wl-sym">{c.symbol}</span>
                      {c.buyers >= 2 && <span style={{ color: "var(--green)", marginLeft: 4 }}>⚑</span>}
                      {c.has_ceo_cfo && <span style={{ color: "var(--green)", marginLeft: 2 }} title="CEO/CFO buy">★</span>}
                    </td>
                    <td className="num">{c.buyers}</td>
                    <td className="num">
                      {c.total_value !== null ? `$${(c.total_value / 1000).toFixed(0)}k` : "—"}
                    </td>
                    <td className="num">{c.last_filed}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
        {!!insider.data && !insider.data.clusters.length && !empty && (
          <div style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 8 }}>
            no watchlist insider buys filed in the last 30d (that itself is information)
          </div>
        )}
      </div>
    </div>
  );
}
