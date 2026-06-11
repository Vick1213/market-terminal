"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { CongressTrade, WhaleFund } from "@market/shared";
import { fetchCongress, fetchWhales } from "@/lib/api";

function money(v: number | null): string {
  if (v === null) return "—";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}bn`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(0)}m`;
  return `$${(v / 1e3).toFixed(0)}k`;
}

function band(t: CongressTrade): string {
  if (t.amount_min === null) return "—";
  if (t.amount_max === null) return `>${money(t.amount_min)}`;
  return `${money(t.amount_min)}–${money(t.amount_max)}`;
}

/** One fund row; clicking toggles the top-holdings drill-down beneath it. */
function FundRow({ f, open, onToggle }: { f: WhaleFund; open: boolean; onToggle: () => void }) {
  return (
    <>
      <tr
        className="wl-row"
        style={{ cursor: "pointer" }}
        onClick={onToggle}
        title={`13F for quarter ending ${f.period}, filed ${f.filed_at} (data lags up to 45d)${
          f.prev_period ? ` — changes vs ${f.prev_period}` : ""
        }. Click to ${open ? "collapse" : "see top holdings"}.`}
      >
        <td className="wl-sym">
          {open ? "▾" : "▸"} {f.fund}
        </td>
        <td className="num">{money(f.total_value)}</td>
        <td className="num">{f.positions ?? "—"}</td>
        <td className="num">
          {f.new_count > 0 && <span style={{ color: "var(--green)" }}>+{f.new_count}</span>}
          {f.new_count > 0 && f.exits.length > 0 && " / "}
          {f.exits.length > 0 && <span style={{ color: "var(--red)" }}>−{f.exits.length}</span>}
          {f.new_count === 0 && f.exits.length === 0 && "—"}
        </td>
        <td className="num">{f.period}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5} style={{ padding: "2px 0 8px 14px" }}>
            {f.holdings.slice(0, 10).map((h) => (
              <div key={`${h.rank}`} style={{ fontSize: 11, display: "flex", gap: 6 }}>
                <span style={{ color: "var(--text-dim)", minWidth: 16 }}>{h.rank}.</span>
                <span style={{ flex: 1 }}>
                  {h.issuer ?? "?"}
                  {h.put_call && (
                    <span style={{ color: "var(--yellow)", marginLeft: 4 }}>{h.put_call}</span>
                  )}
                  {h.status === "new" && (
                    <span style={{ color: "var(--green)", marginLeft: 4 }}>NEW</span>
                  )}
                </span>
                <span className="num" style={{ color: "var(--text-dim)" }}>
                  {h.pct !== null ? `${h.pct}%` : money(h.value)}
                </span>
                <span
                  className="num"
                  style={{
                    minWidth: 48,
                    color:
                      h.shares_chg_pct === null
                        ? "var(--text-dim)"
                        : h.shares_chg_pct >= 0
                          ? "var(--green)"
                          : "var(--red)",
                  }}
                  title="share-count change vs prior quarter"
                >
                  {h.shares_chg_pct === null
                    ? ""
                    : `${h.shares_chg_pct >= 0 ? "+" : ""}${h.shares_chg_pct}%`}
                </span>
              </div>
            ))}
            {f.exits.length > 0 && (
              <div style={{ fontSize: 11, color: "var(--red)", marginTop: 4 }}>
                exited top-{f.holdings.length}: {f.exits.slice(0, 5).join(", ")}
                {f.exits.length > 5 && ` +${f.exits.length - 5} more`}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

export function WhalesPanel() {
  const whales = useQuery({
    queryKey: ["whales"],
    queryFn: fetchWhales,
    refetchInterval: 6 * 60 * 60_000, // quarterly data — refetch is a formality
  });
  const congress = useQuery({
    queryKey: ["congress"],
    queryFn: () => fetchCongress(90),
    refetchInterval: 60 * 60_000,
  });
  const [openCik, setOpenCik] = useState<number | null>(null);

  const anyError = whales.isError && congress.isError;
  const empty = !whales.data?.funds.length && !congress.data?.trades.length;

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Whales — 13F Funds · Congress</span>
        <span
          style={{ fontSize: 10, color: "var(--text-dim)" }}
          title="13F holdings lag up to 45 days; Senate PTRs file within 45 days of the trade. House PTRs are PDF-only and not tracked."
        >
          lagged sources
        </span>
      </div>
      <div className="panel-body">
        {anyError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!anyError && empty && (
          <div style={{ color: "var(--text-dim)" }}>
            warming up — 13F and Senate PTR ingest run ~10 min after boot
          </div>
        )}

        {!!whales.data?.funds.length && (
          <>
            <div className="macro-detail-title">13F whale portfolios (quarterly)</div>
            <table className="wl-table">
              <thead>
                <tr>
                  <th>fund</th>
                  <th className="num">AUM (13F)</th>
                  <th className="num">pos.</th>
                  <th className="num" title="new positions / exits in the stored top-20 vs prior quarter">
                    new/exit
                  </th>
                  <th className="num">quarter</th>
                </tr>
              </thead>
              <tbody>
                {whales.data.funds.map((f) => (
                  <FundRow
                    key={f.cik}
                    f={f}
                    open={openCik === f.cik}
                    onToggle={() => setOpenCik(openCik === f.cik ? null : f.cik)}
                  />
                ))}
              </tbody>
            </table>
          </>
        )}

        {!!congress.data?.trades.length && (
          <>
            <div className="macro-detail-title" style={{ marginTop: 10 }}>
              Senate stock trades (PTRs, 90d)
            </div>
            <table className="wl-table">
              <thead>
                <tr>
                  <th>senator</th>
                  <th>ticker</th>
                  <th>side</th>
                  <th className="num">amount</th>
                  <th className="num">filed</th>
                </tr>
              </thead>
              <tbody>
                {congress.data.trades
                  .filter((t) => t.ticker) // equity rows only in the compact view
                  .slice(0, 14)
                  .map((t) => (
                    <tr
                      key={`${t.ptr_id}:${t.row}`}
                      className="wl-row"
                      title={`${t.asset ?? ""} — traded ${t.tx_date ?? "?"}, filed ${t.filed_at}${
                        t.on_watchlist ? " — ON YOUR WATCHLIST" : ""
                      }`}
                    >
                      <td>{t.senator}</td>
                      <td>
                        <span className="wl-sym">{t.ticker}</span>
                        {t.on_watchlist && (
                          <span style={{ color: "var(--green)", marginLeft: 4 }} title="on your watchlist">
                            ⚑
                          </span>
                        )}
                      </td>
                      <td
                        style={{
                          color:
                            t.side === "buy"
                              ? "var(--green)"
                              : t.side === "sell"
                                ? "var(--red)"
                                : undefined,
                        }}
                      >
                        {t.side ?? "—"}
                      </td>
                      <td className="num">{band(t)}</td>
                      <td className="num">{t.filed_at}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </>
        )}
        {!!congress.data && !congress.data.trades.filter((t) => t.ticker).length && !empty && (
          <div style={{ color: "var(--text-dim)", fontSize: 11, marginTop: 8 }}>
            no senate equity PTRs in the last 90d
          </div>
        )}
      </div>
    </div>
  );
}
