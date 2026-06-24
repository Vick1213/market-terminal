"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { PortfolioPosition, PortfolioSleeve } from "@market/shared";
import { fetchPortfolio } from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";

const AUTO_MS = 30_000;

const SLEEVE_COLOR: Record<PortfolioSleeve, string> = {
  day: "var(--accent, #7aa2f7)",
  swing: "var(--green)",
  manual: "var(--text-dim)",
};

const usd = (v: number | null | undefined, dp = 0) =>
  v === null || v === undefined
    ? "—"
    : `$${v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;

const signedUsd = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v >= 0 ? "+" : "−"}${usd(Math.abs(v))}`;

const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;

function PositionRow({ p, open, onToggle }: { p: PortfolioPosition; open: boolean; onToggle: () => void }) {
  const up = (p.unrealized_pl ?? 0) >= 0;
  const color = up ? "var(--green)" : "var(--red)";
  const shared =
    [p.day_qty > 0 && "day", p.swing_qty > 0 && "swing", p.manual_qty > 0 && "manual"].filter(Boolean)
      .length > 1;
  return (
    <div style={{ borderBottom: "1px solid var(--border, #2a2a2a)", paddingBottom: 3, marginBottom: 3 }}>
      <div
        style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 12 }}
        onClick={onToggle}
      >
        <span style={{ minWidth: 10, color: "var(--text-dim)" }}>{open ? "▾" : "▸"}</span>
        <span style={{ flex: "0 0 72px", fontWeight: 600 }}>{p.symbol}</span>
        <span
          title={shared ? "shares split across sleeves" : `held by the ${p.sleeve_label}`}
          style={{
            flex: "0 0 96px",
            fontSize: 9,
            fontWeight: 700,
            textTransform: "uppercase",
            color: SLEEVE_COLOR[p.sleeve],
            border: `1px solid ${SLEEVE_COLOR[p.sleeve]}`,
            borderRadius: 3,
            padding: "0 4px",
            textAlign: "center",
          }}
        >
          {p.sleeve_label}
          {shared ? " *" : ""}
        </span>
        <span className="num" style={{ flex: "0 0 84px", textAlign: "right" }}>{usd(p.market_value)}</span>
        <span className="num" style={{ flex: 1, textAlign: "right", color, fontWeight: 600 }}>
          {signedUsd(p.unrealized_pl)}
        </span>
        <span className="num" style={{ flex: "0 0 64px", textAlign: "right", color, fontSize: 11 }}>
          {pct(p.unrealized_plpc != null ? p.unrealized_plpc * 100 : null)}
        </span>
      </div>
      {open && (
        <div style={{ padding: "3px 0 2px 18px", fontSize: 11, color: "var(--text-dim)" }}>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 3 }}>
            <span>{p.qty.toLocaleString(undefined, { maximumFractionDigits: 4 })} sh</span>
            <span>avg {usd(p.avg_entry_price, 2)}</span>
            {shared && (
              <span>
                split:{" "}
                {[
                  p.swing_qty > 0 && `swing ${p.swing_qty}`,
                  p.day_qty > 0 && `day ${p.day_qty}`,
                  p.manual_qty > 0 && `manual ${p.manual_qty}`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            )}
          </div>
          {p.exit ? (
            <div style={{ borderLeft: "2px solid var(--border, #2a2a2a)", paddingLeft: 8 }}>
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                <span title="protective stop the owning sleeve implies">
                  <span style={{ color: "var(--red)" }}>stop </span>
                  {usd(p.exit.stop_price, 2)}
                  {p.exit.stop_pct != null ? ` (−${p.exit.stop_pct}%)` : ""}
                </span>
                {p.exit.target_value != null && (
                  <span title="strategist target weight for this name">
                    <span style={{ color: "var(--green)" }}>target </span>
                    {usd(p.exit.target_value)}
                    {p.exit.target_pct != null ? ` (${p.exit.target_pct}%)` : ""}
                  </span>
                )}
              </div>
              {p.exit.invalidation && (
                <div style={{ marginTop: 2 }}>
                  <span style={{ color: "var(--yellow)" }}>exit when: </span>
                  {p.exit.invalidation}
                </div>
              )}
            </div>
          ) : (
            <div style={{ fontStyle: "italic" }}>no bot exit plan (manual position)</div>
          )}
        </div>
      )}
    </div>
  );
}

export function PortfolioPanel() {
  const [auto, setAuto] = useState(true);
  const [openSym, setOpenSym] = useState<string | null>(null);
  const [winnersOnly, setWinnersOnly] = useState(false);

  const q = useQuery({
    queryKey: ["portfolio"],
    queryFn: fetchPortfolio,
    refetchInterval: auto ? AUTO_MS : false,
  });

  // live-refresh on any bot WS event (a fill changes the portfolio)
  const { last } = useWebSocket("bot", 1);
  useEffect(() => {
    if (last && auto) q.refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [last]);

  const d = q.data;
  const off = d && !d.broker.enabled;
  const total = d?.total_value;
  const dayUp = (d?.day_pnl_pct ?? 0) >= 0;
  const uplUp = (d?.unrealized_pl ?? 0) >= 0;
  const sleeveOrder: PortfolioSleeve[] = ["swing", "day", "manual"];
  const list = (d?.positions ?? []).filter((p) => (winnersOnly ? p.winning : true));

  return (
    <div className="panel">
      <div className="panel-head">
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          Portfolio
          {d && (
            <span
              style={{
                fontSize: 9,
                padding: "1px 5px",
                borderRadius: 3,
                fontWeight: 700,
                color: d.broker.is_paper ? "var(--green)" : "var(--red)",
                border: `1px solid ${d.broker.is_paper ? "var(--green)" : "var(--red)"}`,
              }}
              title={d.broker.base_url}
            >
              {d.broker.is_paper ? "PAPER" : "LIVE"}
            </span>
          )}
          {q.isFetching && <span style={{ fontSize: 10, color: "var(--text-dim)" }}>· syncing…</span>}
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <button
            className="expand-btn"
            title={auto ? `Auto-refresh ON (every ${AUTO_MS / 1000}s + on fills) — click to pause` : "Auto-refresh OFF — click to resume"}
            onClick={(e) => {
              e.stopPropagation();
              setAuto((v) => !v);
            }}
            style={{ color: auto ? "var(--green)" : "var(--text-dim)", fontWeight: 600 }}
          >
            {auto ? "● auto" : "○ auto"}
          </button>
          <button
            className="expand-btn"
            title="Refresh now from the broker (paper account ground truth)"
            disabled={q.isFetching}
            onClick={(e) => {
              e.stopPropagation();
              q.refetch();
            }}
          >
            {q.isFetching ? "…" : "↻ refresh"}
          </button>
        </span>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {off && (
          <div style={{ color: "var(--yellow)", fontSize: 11 }}>
            {d?.detail ?? "Alpaca paper keys not configured."} Set MARKET_ALPACA_PAPER_KEY_ID / SECRET
            and restart the API.
          </div>
        )}
        {d && d.ok && (
          <>
            {/* the big total value */}
            <div style={{ textAlign: "center", padding: "10px 0 6px" }}>
              <div style={{ fontSize: 11, color: "var(--text-dim)", letterSpacing: 1, textTransform: "uppercase" }}>
                Total portfolio value
              </div>
              <div className="num" style={{ fontSize: 44, fontWeight: 700, lineHeight: 1.1 }}>
                {usd(total)}
              </div>
              <div style={{ display: "flex", justifyContent: "center", gap: 16, marginTop: 4, fontSize: 12 }}>
                <span title="today's P&L vs previous close">
                  <span style={{ color: "var(--text-dim)" }}>today </span>
                  <span className="num" style={{ color: dayUp ? "var(--green)" : "var(--red)", fontWeight: 600 }}>
                    {pct(d.day_pnl_pct)}
                  </span>
                </span>
                <span title="open unrealized P&L across all positions">
                  <span style={{ color: "var(--text-dim)" }}>unrealized </span>
                  <span className="num" style={{ color: uplUp ? "var(--green)" : "var(--red)", fontWeight: 600 }}>
                    {signedUsd(d.unrealized_pl)} ({pct(d.unrealized_pl_pct)})
                  </span>
                </span>
              </div>
              <div style={{ marginTop: 4, fontSize: 10, color: "var(--text-dim)" }}>
                cash {usd(d.cash)} · buying power {usd(d.buying_power)} · {d.n_winners}↑ / {d.n_losers}↓ of{" "}
                {d.n_positions} positions
              </div>
            </div>

            {/* per-bot rollup */}
            <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
              {sleeveOrder.map((k) => {
                const s = d.sleeves?.[k];
                if (!s || (s.n === 0 && s.value === 0)) return null;
                const up = s.unrealized_pl >= 0;
                return (
                  <div
                    key={k}
                    style={{
                      flex: 1,
                      border: `1px solid var(--border, #2a2a2a)`,
                      borderTop: `2px solid ${SLEEVE_COLOR[k]}`,
                      borderRadius: 3,
                      padding: "4px 6px",
                    }}
                  >
                    <div style={{ fontSize: 10, color: SLEEVE_COLOR[k], fontWeight: 600 }}>{s.label}</div>
                    <div className="num" style={{ fontSize: 14, fontWeight: 600 }}>{usd(s.value)}</div>
                    <div className="num" style={{ fontSize: 10, color: up ? "var(--green)" : "var(--red)" }}>
                      {signedUsd(s.unrealized_pl)} · {s.n} pos
                    </div>
                  </div>
                );
              })}
            </div>

            <div className="macro-detail-title" style={{ display: "flex", alignItems: "center" }}>
              <span>Positions {winnersOnly ? "(winners)" : "(by P&L)"}</span>
              <button
                className="expand-btn"
                style={{ marginLeft: "auto", color: winnersOnly ? "var(--green)" : "var(--text-dim)" }}
                title="Show only winning trades"
                onClick={() => setWinnersOnly((v) => !v)}
              >
                {winnersOnly ? "● winners" : "○ winners"}
              </button>
            </div>
            {!list.length && (
              <div style={{ color: "var(--text-dim)", fontSize: 11 }}>
                {winnersOnly ? "no winning trades yet" : "no open positions"}
              </div>
            )}
            {list.map((p) => (
              <PositionRow
                key={p.symbol}
                p={p}
                open={openSym === p.symbol}
                onToggle={() => setOpenSym(openSym === p.symbol ? null : p.symbol)}
              />
            ))}

            <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 8 }}>{d.disclaimer}</div>
          </>
        )}
      </div>
    </div>
  );
}
