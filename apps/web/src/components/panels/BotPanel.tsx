"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  BotProposal,
  BotPosition,
  DayProposal,
  DayProposalRationale,
  MarketContext,
  PortfolioState,
  TradeLevels,
} from "@market/shared";
import { TradeChart } from "@/components/charts/TradeChart";
import {
  executeBotProposal,
  fetchBotStatus,
  fetchDayStatus,
  fetchTrades,
  reconcileBot,
  runBotPropose,
  runDay,
  setBotEnabled,
  setBotMode,
  setManagedExits,
  setPostureSizing,
  setDayEnabled,
  setDayHedge,
  setDaySoftStop,
  setDayShorts,
  setDayPairs,
} from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";

const SIDE_COLOR: Record<string, string> = {
  buy: "var(--green)",
  sell: "var(--red)",
};

const STATUS_COLOR: Record<string, string> = {
  proposed: "var(--text, #ddd)",
  submitted: "var(--accent, #7aa2f7)",
  submitting: "var(--accent, #7aa2f7)",
  filled: "var(--green)",
  blocked: "var(--yellow)",
  skipped: "var(--text-dim)",
  rejected: "var(--red)",
  unknown: "var(--yellow)",
  canceled: "var(--text-dim)",
  expired: "var(--text-dim)",
};

const usd = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;

// green for risk-on/neutral, red for risk-off/stress
const regimeColor = (regime: string | null | undefined) => {
  const r = (regime ?? "").toLowerCase();
  if (r.includes("stress") || r.includes("risk-off") || r.includes("off")) return "var(--red)";
  if (r.includes("risk-on") || r.includes("on") || r.includes("neutral")) return "var(--green)";
  return "var(--text-dim)";
};

// Pull entry / take-profit / stop-loss out of a day proposal's hedged-bracket
// rationale (the "primary" leg carries the tradeable prices).
function dayTradeLevels(p: DayProposal): TradeLevels | null {
  const r = (p.rationale ?? null) as DayProposalRationale | null;
  const legs = r?.legs ?? [];
  const primary = legs.find((l) => l.role === "primary") ?? legs[0];
  if (!primary) return null;
  const lv: TradeLevels = {
    entry: primary.entry ?? null,
    tp: primary.tp_price ?? null,
    sl: primary.sl_price ?? null,
  };
  return lv.entry == null && lv.tp == null && lv.sl == null ? null : lv;
}

function ProposalRow({
  p,
  canExecute,
  open,
  onToggle,
  onExecute,
  executing,
  entryPrice,
}: {
  p: BotProposal;
  canExecute: boolean;
  open: boolean;
  onToggle: () => void;
  onExecute: () => void;
  executing: boolean;
  entryPrice?: number | null;
}) {
  const size = p.side === "buy" ? usd(p.notional) : `${(p.qty ?? 0).toLocaleString(undefined, { maximumFractionDigits: 4 })} sh`;
  const actionable = p.status === "proposed" && !p.stale;
  return (
    <div style={{ marginBottom: 3, borderBottom: "1px solid var(--border, #2a2a2a)", paddingBottom: 3, opacity: p.stale ? 0.5 : 1 }}>
      <div
        style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 12 }}
        onClick={onToggle}
      >
        <span style={{ minWidth: 10, color: "var(--text-dim)" }}>{open ? "▾" : "▸"}</span>
        <span
          className="num"
          style={{ flex: "0 0 38px", color: SIDE_COLOR[p.side] ?? "var(--text-dim)", fontWeight: 600 }}
        >
          {p.side.toUpperCase()}
        </span>
        <span style={{ flex: "0 0 72px", fontWeight: 600 }}>{p.symbol}</span>
        <span className="num" style={{ flex: "0 0 84px", textAlign: "right" }}>{size}</span>
        <span
          className="num"
          style={{ flex: "0 0 96px", textAlign: "right", fontSize: 10, color: "var(--text-dim)" }}
          title="current % of equity → strategist target %"
        >
          {(p.current_pct ?? 0).toFixed(1)}→{(p.target_pct ?? 0).toFixed(1)}%
        </span>
        <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 4 }}>
          {p.stale && (
            <span
              style={{ fontSize: 9, color: "var(--yellow)", border: "1px solid var(--yellow)",
                borderRadius: 3, padding: "0 4px" }}
              title="Fresh account state no longer calls for this — you've already done it. Re-propose (↻)."
            >
              STALE
            </span>
          )}
          <span style={{ fontSize: 10, textTransform: "uppercase", color: STATUS_COLOR[p.status] ?? "var(--text-dim)" }}>
            {p.status}
          </span>
        </span>
        {canExecute && actionable && (
          <button
            className="expand-btn"
            title={`Submit PAPER ${p.side} ${p.symbol}`}
            style={{ color: SIDE_COLOR[p.side], fontWeight: 700 }}
            disabled={executing}
            onClick={(e) => {
              e.stopPropagation();
              if (
                window.confirm(
                  `Submit PAPER ${p.side.toUpperCase()} ${p.symbol} (${size})?\n\nThis places a real order on your Alpaca PAPER account.`,
                )
              ) {
                onExecute();
              }
            }}
          >
            {executing ? "…" : "▶ exec"}
          </button>
        )}
      </div>
      {open && (
        <div style={{ padding: "3px 0 2px 16px", fontSize: 11, color: "var(--text-dim)" }}>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 2 }}>
            {p.conviction != null && <span>conviction {p.conviction}</span>}
            <span title="illustrative downside on the resulting position, not a real stop">
              max-loss est {usd(p.max_loss_est)}
            </span>
            <span>Δ {usd(p.delta_value)}</span>
            {p.bucket && <span>{p.bucket}</span>}
          </div>
          {!!p.rationale?.evidence?.length && (
            <div style={{ marginBottom: 2 }}>
              <span style={{ color: "var(--green)" }}>bull: </span>
              {p.rationale.evidence.join("; ")}
            </div>
          )}
          {p.rationale?.bear_case && (
            <div style={{ marginBottom: 2 }}>
              <span style={{ color: "var(--red)" }}>bear: </span>
              {p.rationale.bear_case}
            </div>
          )}
          {p.rationale?.invalidation && (
            <div style={{ marginBottom: 2 }}>
              <span style={{ color: "var(--yellow)" }}>invalidation: </span>
              {p.rationale.invalidation}
            </div>
          )}
          {!!p.blocks?.length && (
            <div style={{ color: "var(--yellow)" }}>⛔ {p.blocks.join(" · ")}</div>
          )}
          {open && (
            <TradeChart
              symbol={p.symbol}
              levels={entryPrice != null ? { entry: entryPrice } : null}
            />
          )}
        </div>
      )}
    </div>
  );
}

// Compact market + portfolio context for the day sleeve. Every field may be
// absent on older API responses, so guard each one.
function ContextStrip({
  market,
  portfolio,
}: {
  market?: MarketContext | null;
  portfolio?: PortfolioState | null;
}) {
  if (!market && !portfolio) return null;
  const chip = {
    fontSize: 9,
    padding: "0 4px",
    borderRadius: 3,
    border: "1px solid var(--border, #2a2a2a)",
  } as const;
  const sectors = portfolio
    ? Object.entries(portfolio.sector_exposure ?? {})
        .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
        .slice(0, 4)
    : [];
  const budgetUsed =
    portfolio && portfolio.day_budget > 0
      ? Math.min(1, Math.max(0, portfolio.day_value / portfolio.day_budget))
      : null;
  const betaHot = portfolio != null && Math.abs(portfolio.net_beta_pct) > 70;

  return (
    <div
      style={{
        fontSize: 10,
        marginBottom: 6,
        padding: "4px 6px",
        borderRadius: 3,
        border: "1px solid var(--border, #2a2a2a)",
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      {market && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "baseline" }}>
          <span style={{ fontSize: 9, color: "var(--text-dim)", flex: "0 0 auto" }}>mkt</span>
          <span style={{ fontWeight: 700, color: regimeColor(market.regime) }}>
            {(market.regime || "—").toUpperCase()}
          </span>
          <span
            style={{
              ...chip,
              color: market.risk_on ? "var(--green)" : "var(--red)",
              borderColor: market.risk_on ? "var(--green)" : "var(--red)",
            }}
          >
            {market.risk_on ? "RISK-ON" : "RISK-OFF"}
          </span>
          {market.stress_on && (
            <span style={{ ...chip, color: "var(--red)", borderColor: "var(--red)" }}>STRESS</span>
          )}
          {market.composite_score != null && (
            <span className="num" style={{ color: "var(--text-dim)" }}>
              score {market.composite_score.toFixed(0)}
            </span>
          )}
          {market.breadth_pct != null && (
            <span className="num" style={{ color: "var(--text-dim)" }}>
              breadth {market.breadth_pct.toFixed(0)}%
            </span>
          )}
          <span style={{ color: "var(--text-dim)" }}>
            γ{" "}
            <span
              style={{
                color:
                  market.dealer_gamma === "short"
                    ? "var(--red)"
                    : market.dealer_gamma === "long"
                      ? "var(--green)"
                      : "var(--text-dim)",
              }}
            >
              {market.dealer_gamma}
            </span>
          </span>
          {market.vol_pctile != null && (
            <span className="num" style={{ color: "var(--text-dim)" }}>
              vol {Math.round(market.vol_pctile)}%-ile
              {market.vol_regime ? ` ${market.vol_regime}` : ""}
            </span>
          )}
          {market.suggested_exposure != null && (
            <span className="num" style={{ color: "var(--text-dim)" }}>
              exp ×{market.suggested_exposure.toFixed(2)}
            </span>
          )}
        </div>
      )}
      {market && (!!market.strategist_favor?.length || !!market.strategist_avoid?.length) && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, color: "var(--text-dim)" }}>
          {!!market.strategist_favor?.length && (
            <span>
              <span style={{ color: "var(--green)" }}>favor:</span> {market.strategist_favor.join(", ")}
            </span>
          )}
          {!!market.strategist_avoid?.length && (
            <span>
              <span style={{ color: "var(--red)" }}>avoid:</span> {market.strategist_avoid.join(", ")}
            </span>
          )}
        </div>
      )}
      {market && !!market.notes?.length && (
        <div
          style={{
            color: "var(--text-dim)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={market.notes.join(" · ")}
        >
          {market.notes.join(" · ")}
        </div>
      )}
      {portfolio && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "baseline" }}>
          <span style={{ fontSize: 9, color: "var(--text-dim)", flex: "0 0 auto" }}>pf</span>
          <span className="num" style={{ color: "var(--text-dim)" }}>
            equity {usd(portfolio.equity)}
          </span>
          <span className="num" style={{ color: "var(--text-dim)" }}>
            cash {usd(portfolio.cash)}
          </span>
          <span className="num" style={{ fontWeight: 700, color: betaHot ? "var(--red)" : "var(--text)" }}>
            net β {portfolio.net_beta_pct.toFixed(0)}%
          </span>
          {portfolio.day_budget > 0 && (
            <span
              className="num"
              style={{ color: "var(--text-dim)", display: "flex", alignItems: "center", gap: 4 }}
              title={`day sleeve deployed ${usd(portfolio.day_value)} of ${usd(portfolio.day_budget)} budget`}
            >
              day {usd(portfolio.day_value)}/{usd(portfolio.day_budget)}
              {budgetUsed != null && (
                <span
                  style={{
                    display: "inline-block",
                    width: 40,
                    height: 5,
                    borderRadius: 3,
                    background: "var(--border, #2a2a2a)",
                    overflow: "hidden",
                  }}
                >
                  <span
                    style={{
                      display: "block",
                      height: "100%",
                      width: `${budgetUsed * 100}%`,
                      background: budgetUsed > 0.9 ? "var(--red)" : "var(--accent, #7aa2f7)",
                    }}
                  />
                </span>
              )}
            </span>
          )}
        </div>
      )}
      {portfolio && !!sectors.length && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, color: "var(--text-dim)" }}>
          {sectors.map(([sector, value]) => (
            <span key={sector} className="num">
              {sector} {usd(value)}
              {portfolio.equity > 0 && ` (${((value / portfolio.equity) * 100).toFixed(0)}%)`}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function DaySection() {
  const qc = useQueryClient();
  const [openDayId, setOpenDayId] = useState<number | null>(null);
  const q = useQuery({ queryKey: ["day"], queryFn: fetchDayStatus, refetchInterval: 15_000 });
  const inv = () => {
    qc.invalidateQueries({ queryKey: ["day"] });
    qc.invalidateQueries({ queryKey: ["bot"] });
  };
  // The day bot broadcasts a "bot" WS event after every tick / order — refresh
  // live on it (the swing section already does this) so fills/exits show up
  // without a manual reload.
  const { last } = useWebSocket("bot", 1);
  useEffect(() => {
    if (last) inv();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [last]);
  const toggle = useMutation({ mutationFn: setDayEnabled, onSuccess: inv });
  const hedgeToggle = useMutation({ mutationFn: setDayHedge, onSuccess: inv });
  const softStopToggle = useMutation({ mutationFn: setDaySoftStop, onSuccess: inv });
  const shortsToggle = useMutation({ mutationFn: setDayShorts, onSuccess: inv });
  const pairsToggle = useMutation({ mutationFn: setDayPairs, onSuccess: inv });
  const run = useMutation({ mutationFn: runDay, onSuccess: inv });
  const d = q.data;
  const enabled = !!d?.config.enabled;
  const hedgeOn = !!d?.config.hedge_enabled;
  const softStop = !!d?.config.soft_stop;
  const shortsOn = !!d?.config.short_enabled;
  const pairsOn = !!d?.config.pairs_enabled;

  return (
    <div style={{ marginTop: 10, borderTop: "1px solid var(--border, #2a2a2a)", paddingTop: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <span style={{ fontWeight: 600 }}>Day trader</span>
        <span style={{ fontSize: 9, color: "var(--accent, #7aa2f7)" }}>fast · auto</span>
        {d?.market_open !== undefined && (
          <span style={{ fontSize: 10, color: d.market_open ? "var(--green)" : "var(--text-dim)" }}>
            {d.market_open ? "market open" : "closed (crypto only)"}
          </span>
        )}
        <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button className="expand-btn" disabled={run.isPending} title="Run a day-trade tick now"
            onClick={() => run.mutate()}>{run.isPending ? "…" : "↻"}</button>
          <button
            onClick={() => shortsToggle.mutate(!shortsOn)}
            disabled={shortsToggle.isPending}
            title={shortsOn
              ? "SHORTS ON — day bot may open short entries (equities only, mandatory upside stop). Click to disable."
              : "Shorts OFF — day bot is long-only. Click to allow short entries (equities only)."}
            style={{
              fontSize: 11, fontWeight: 700, padding: "1px 7px", borderRadius: 3, cursor: "pointer",
              background: "transparent",
              border: `1px solid ${shortsOn ? "var(--red)" : "var(--text-dim, #666)"}`,
              color: shortsOn ? "var(--red)" : "var(--text-dim, #666)",
            }}
          >
            {shortsOn ? "⇅ L/S" : "⇅ LONG"}
          </button>
          <button
            onClick={() => pairsToggle.mutate(!pairsOn)}
            disabled={pairsToggle.isPending || !shortsOn}
            title={!shortsOn
              ? "Pairs needs shorts armed first (the short leg)"
              : pairsOn
                ? "PAIRS ON — relative-strength market-neutral trades (long strong / short weak). Click to disable."
                : "Pairs OFF — click to allow relative-strength market-neutral pair trades"}
            style={{
              fontSize: 11, fontWeight: 700, padding: "1px 7px", borderRadius: 3,
              cursor: shortsOn ? "pointer" : "not-allowed", background: "transparent",
              border: `1px solid ${pairsOn ? "var(--accent, #7aa2f7)" : "var(--text-dim, #666)"}`,
              color: pairsOn ? "var(--accent, #7aa2f7)" : "var(--text-dim, #666)",
              opacity: shortsOn ? 1 : 0.5,
            }}
          >
            {pairsOn ? "⤬ PAIRS" : "⤬ PAIRS"}
          </button>
          <button
            onClick={() => softStopToggle.mutate(!softStop)}
            disabled={softStopToggle.isPending}
            title={softStop
              ? "SOFT STOP ON — no new entries; open positions still managed (exits/stops run). Click to resume."
              : "Soft stop — pause NEW entries but keep managing open positions"}
            style={{
              fontSize: 11, fontWeight: 700, padding: "1px 7px", borderRadius: 3, cursor: "pointer",
              background: "transparent",
              border: `1px solid ${softStop ? "var(--yellow)" : "var(--text-dim, #666)"}`,
              color: softStop ? "var(--yellow)" : "var(--text-dim, #666)",
            }}
          >
            {softStop ? "⏸ PAUSED" : "⏸ SOFT STOP"}
          </button>
          <button
            onClick={() => hedgeToggle.mutate(!hedgeOn)}
            disabled={hedgeToggle.isPending}
            title={hedgeOn
              ? "Net-beta SH hedge ON — rebalances a single SH (-1x S&P) position to the day book's net beta"
              : "Net-beta SH hedge OFF — SH is a weak unlevered hedge; click to arm"}
            style={{
              fontSize: 11, fontWeight: 700, padding: "1px 7px", borderRadius: 3, cursor: "pointer",
              background: "transparent",
              border: `1px solid ${hedgeOn ? "var(--accent, #7aa2f7)" : "var(--text-dim, #666)"}`,
              color: hedgeOn ? "var(--accent, #7aa2f7)" : "var(--text-dim, #666)",
            }}
          >
            {hedgeOn ? "⛨ SH HEDGE" : "⛨ SH OFF"}
          </button>
          <button
            onClick={() => toggle.mutate(!enabled)}
            disabled={toggle.isPending}
            title={enabled ? "Day bot ARMED — auto-trading the day sleeve (paper)" : "Day bot halted — click to arm"}
            style={{
              fontSize: 11, fontWeight: 700, padding: "1px 7px", borderRadius: 3, cursor: "pointer",
              background: "transparent",
              border: `1px solid ${enabled ? "var(--green)" : "var(--red)"}`,
              color: enabled ? "var(--green)" : "var(--red)",
            }}
          >
            {enabled ? "● ARMED" : "○ HALTED"}
          </button>
        </span>
      </div>
      {d && (
        <>
          <div style={{ fontSize: 10, color: "var(--text-dim)", marginBottom: 4 }}>
            {d.universe.join(", ")} · cap {d.guardrails?.max_position_pct}% of day sleeve · daily-loss
            {" "}-{d.guardrails?.daily_loss_limit_pct}%
          </div>
          {d.intraday_plan && (
            <div
              style={{ fontSize: 10, marginBottom: 5, display: "flex", flexWrap: "wrap",
                gap: 10, alignItems: "baseline" }}
              title={d.intraday_plan.note}
            >
              <span style={{ fontSize: 9, color: "var(--text-dim)" }}>plan</span>
              <span style={{ fontWeight: 700,
                color: d.intraday_plan.bias === "risk-on" ? "var(--green)"
                  : d.intraday_plan.bias === "risk-off" ? "var(--red)" : "var(--yellow)" }}>
                {d.intraday_plan.bias.toUpperCase()}
              </span>
              <span className="num">stop −{d.intraday_plan.stop_pct}% / tp +{d.intraday_plan.tp_pct}%</span>
              <span className="num">size ×{d.intraday_plan.risk_scale}</span>
              <span style={{ color: d.intraday_plan.require_hedge
                ? "var(--accent, #7aa2f7)" : "var(--text-dim)" }}>
                {d.intraday_plan.require_hedge ? "hedge required" : "hedge optional"}
              </span>
              {d.intraday_plan.vol_percentile != null && (
                <span style={{ color: "var(--text-dim)" }}>
                  vol {Math.round(d.intraday_plan.vol_percentile * 100)}%-ile
                </span>
              )}
            </div>
          )}
          <ContextStrip market={d.market_context} portfolio={d.portfolio_state} />
          {d.recent_proposals?.length ? (
            d.recent_proposals.slice(0, 8).map((p) => {
              const r = (p.rationale ?? null) as { reason?: string; kind?: string } | null;
              const isOpen = openDayId === p.id;
              const levels = dayTradeLevels(p);
              return (
                <div key={p.id}>
                  <div
                    style={{ display: "flex", gap: 6, fontSize: 11, alignItems: "baseline", cursor: "pointer" }}
                    onClick={() => setOpenDayId(isOpen ? null : p.id)}
                  >
                    <span style={{ minWidth: 9, color: "var(--text-dim)", fontSize: 10 }}>
                      {isOpen ? "▾" : "▸"}
                    </span>
                    <span className="num" style={{ flex: "0 0 38px", fontWeight: 600,
                      color: p.side === "buy" ? "var(--green)" : "var(--red)" }}>{p.side.toUpperCase()}</span>
                    <span style={{ flex: "0 0 70px", fontWeight: 600 }}>{p.symbol}</span>
                    <span style={{ flex: "0 0 60px", fontSize: 10, color: STATUS_COLOR[p.status] ?? "var(--text-dim)" }}>
                      {p.status}
                    </span>
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                      fontSize: 10, color: "var(--text-dim)" }}>
                      {r?.reason ?? r?.kind ?? ""}
                    </span>
                  </div>
                  {/* Broker rejection reason — so a rejected proposal explains itself. */}
                  {p.error && (
                    <div
                      style={{ paddingLeft: 24, fontSize: 10, color: "var(--red)", overflow: "hidden",
                        textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      title={p.error}
                    >
                      ✗ rejected: {p.error}
                    </div>
                  )}
                  {isOpen && (
                    <div style={{ padding: "2px 0 4px 15px" }}>
                      <TradeChart symbol={p.symbol} levels={levels} intraday />
                    </div>
                  )}
                </div>
              );
            })
          ) : (
            <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
              {enabled ? "armed — waiting for an intraday setup" : "halted — arm to trade the day sleeve (paper)"}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function BotPanel() {
  const queryClient = useQueryClient();
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["bot"] });
  const q = useQuery({ queryKey: ["bot"], queryFn: fetchBotStatus, refetchInterval: 60_000 });

  // live-refresh on any bot WS event (propose/execute/config)
  const { last } = useWebSocket("bot", 1);
  useEffect(() => {
    if (last) invalidate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [last]);

  const propose = useMutation({ mutationFn: runBotPropose, onSuccess: invalidate });
  const reconcile = useMutation({ mutationFn: reconcileBot, onSuccess: invalidate });
  const toggleEnabled = useMutation({ mutationFn: setBotEnabled, onSuccess: invalidate });
  const toggleMode = useMutation({ mutationFn: setBotMode, onSuccess: invalidate });
  const toggleManagedExits = useMutation({ mutationFn: setManagedExits, onSuccess: invalidate });
  const togglePostureSizing = useMutation({ mutationFn: setPostureSizing, onSuccess: invalidate });
  const execute = useMutation({
    mutationFn: executeBotProposal,
    onSuccess: (r) => {
      invalidate();
      if (r && r.ok === false) window.alert(`Not executed: ${r.detail ?? "blocked"}`);
    },
  });

  // Tradebook join: per-symbol closed/open status so a "filled" order whose
  // position has since been CLOSED reads "closed · +$pnl" instead of "filled".
  const tradesQ = useQuery({
    queryKey: ["trades", "swing"],
    queryFn: () => fetchTrades("swing"),
    refetchInterval: 30_000,
  });
  const symbolPnl = useMemo(() => {
    const m = new Map<string, { hasOpen: boolean; hasClosed: boolean; pnl: number }>();
    for (const t of tradesQ.data?.trades ?? []) {
      const e = m.get(t.symbol) ?? { hasOpen: false, hasClosed: false, pnl: 0 };
      if (t.status === "open") e.hasOpen = true;
      else {
        e.hasClosed = true;
        e.pnl += t.pnl ?? 0;
      }
      m.set(t.symbol, e);
    }
    return m;
  }, [tradesQ.data]);

  const [openId, setOpenId] = useState<number | null>(null);
  const d = q.data;

  const brokerOff = d && !d.broker.enabled;
  const enabled = !!d?.config.enabled;
  const mode = d?.config.mode ?? "proposal";
  const managedExits = !!d?.config.managed_exits;
  const postureSizing = !!d?.config.posture_sizing;
  const acct = d?.account;
  const dailyLimit = d?.guardrails.daily_loss_limit_pct ?? 0;
  const halted =
    acct?.day_pnl_pct != null && dailyLimit > 0 && acct.day_pnl_pct <= -dailyLimit;

  return (
    <div className="panel">
      <div className="panel-head">
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          Bot
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
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <button
            className="expand-btn"
            title="Reconcile order state from the broker (ground truth)"
            disabled={reconcile.isPending || brokerOff}
            onClick={(e) => {
              e.stopPropagation();
              reconcile.mutate();
            }}
          >
            {reconcile.isPending ? "…" : "⇄"}
          </button>
          <button
            className="expand-btn"
            title="Generate proposals now from the latest strategist snapshot"
            disabled={propose.isPending || brokerOff}
            onClick={(e) => {
              e.stopPropagation();
              propose.mutate();
            }}
          >
            {propose.isPending ? "…" : "↻"}
          </button>
        </span>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {brokerOff && (
          <div style={{ color: "var(--yellow)", fontSize: 11, marginBottom: 6 }}>
            Alpaca paper keys not configured. Set MARKET_ALPACA_PAPER_KEY_ID / SECRET (or reuse
            MARKET_ALPACA_* if those are paper keys) and restart the API.
          </div>
        )}

        {d && (
          <>
            {/* control bar */}
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <button
                onClick={() => toggleEnabled.mutate(!enabled)}
                disabled={toggleEnabled.isPending || brokerOff}
                title={enabled ? "Kill switch ON — click to halt (cancels open orders)" : "Bot halted — click to arm"}
                style={{
                  fontSize: 11,
                  fontWeight: 700,
                  padding: "2px 8px",
                  borderRadius: 3,
                  cursor: "pointer",
                  border: `1px solid ${enabled ? "var(--green)" : "var(--red)"}`,
                  color: enabled ? "var(--green)" : "var(--red)",
                  background: "transparent",
                }}
              >
                {enabled ? "● ARMED" : "○ HALTED"}
              </button>
              <button
                onClick={() => toggleMode.mutate(mode === "auto" ? "proposal" : "auto")}
                disabled={toggleMode.isPending || brokerOff}
                title="proposal = approve each order by hand; auto = run() submits all actionable (paper)"
                style={{
                  fontSize: 11,
                  padding: "2px 8px",
                  borderRadius: 3,
                  cursor: "pointer",
                  border: "1px solid var(--border, #2a2a2a)",
                  color: mode === "auto" ? "var(--yellow)" : "var(--text-dim)",
                  background: "transparent",
                }}
              >
                mode: {mode}
              </button>
              <button
                onClick={() => toggleManagedExits.mutate(!managedExits)}
                disabled={toggleManagedExits.isPending || brokerOff}
                title="Enforced protective exits: stop-loss / trailing / profit-take / RRG rotation (needs the env master swing_managed_exits on too)"
                style={{
                  fontSize: 11,
                  padding: "2px 8px",
                  borderRadius: 3,
                  cursor: "pointer",
                  border: `1px solid ${managedExits ? "var(--green)" : "var(--border, #2a2a2a)"}`,
                  color: managedExits ? "var(--green)" : "var(--text-dim)",
                  background: "transparent",
                }}
              >
                exits: {managedExits ? "on" : "off"}
              </button>
              <button
                onClick={() => togglePostureSizing.mutate(!postureSizing)}
                disabled={togglePostureSizing.isPending || brokerOff}
                title="Posture-scaled gross + per-sector cap when sizing proposals (needs the env master swing_posture_sizing on too)"
                style={{
                  fontSize: 11,
                  padding: "2px 8px",
                  borderRadius: 3,
                  cursor: "pointer",
                  border: `1px solid ${postureSizing ? "var(--green)" : "var(--border, #2a2a2a)"}`,
                  color: postureSizing ? "var(--green)" : "var(--text-dim)",
                  background: "transparent",
                }}
              >
                posture: {postureSizing ? "on" : "off"}
              </button>
              {acct && (
                <span style={{ marginLeft: "auto", fontSize: 11 }}>
                  <span style={{ color: "var(--text-dim)" }}>equity </span>
                  <span className="num">{usd(acct.equity)}</span>
                  <span
                    className="num"
                    style={{
                      marginLeft: 6,
                      color: halted
                        ? "var(--red)"
                        : (acct.day_pnl_pct ?? 0) >= 0
                          ? "var(--green)"
                          : "var(--red)",
                    }}
                    title="today's P&L vs previous close"
                  >
                    {pct(acct.day_pnl_pct)}
                  </span>
                </span>
              )}
            </div>

            {d.optimizer && (
              <div style={{ marginBottom: 6 }} title={d.optimizer.reason}>
                <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 10, color: "var(--text-dim)" }}>
                  <span>capital split</span>
                  <span style={{ color: "var(--text-dim)" }}>· {d.optimizer.regime ?? "—"}</span>
                  <span style={{ marginLeft: "auto" }} className="num">
                    swing {d.optimizer.swing_pct.toFixed(0)}% / day {d.optimizer.day_pct.toFixed(0)}%
                  </span>
                </div>
                <div style={{ display: "flex", height: 6, borderRadius: 3, overflow: "hidden", marginTop: 2 }}>
                  <div style={{ width: `${d.optimizer.swing_pct}%`, background: "var(--green)", opacity: 0.7 }} />
                  <div style={{ width: `${d.optimizer.day_pct}%`, background: "var(--accent, #7aa2f7)" }} />
                </div>
              </div>
            )}

            {halted && (
              <div style={{ color: "var(--red)", fontSize: 11, marginBottom: 4 }}>
                daily-loss circuit breaker tripped ({pct(acct?.day_pnl_pct)} ≤ -{dailyLimit}%) — buys blocked
              </div>
            )}

            {acct && (
              <div style={{ fontSize: 10, color: "var(--text-dim)", marginBottom: 4 }}>
                cash {usd(acct.cash)} · buying power {usd(acct.buying_power)} · caps:{" "}
                {d.guardrails.max_position_pct}% / {usd(d.guardrails.max_position_notional)} per name ·
                daily-loss -{d.guardrails.daily_loss_limit_pct}%
                {d.account_error ? ` · ⚠ ${d.account_error}` : ""}
              </div>
            )}

            <div className="macro-detail-title">
              Proposals {mode === "auto" && enabled ? "(auto-exec on run)" : "(click ▶ to submit each)"}
            </div>
            {!d.proposals.length && (
              <div style={{ color: "var(--text-dim)", fontSize: 11 }}>
                no open proposals — hit ↻ to generate from the latest strategist snapshot
              </div>
            )}
            {d.proposals.map((p) => (
              <ProposalRow
                key={p.id}
                p={p}
                canExecute={enabled && !brokerOff}
                open={openId === p.id}
                onToggle={() => setOpenId(openId === p.id ? null : p.id)}
                onExecute={() => execute.mutate(p.id)}
                executing={execute.isPending && execute.variables === p.id}
                entryPrice={
                  (d.positions ?? []).find((pos: BotPosition) => pos.symbol === p.symbol)
                    ?.avg_entry_price ?? null
                }
              />
            ))}

            {!!d.positions?.length && (
              <>
                <div className="macro-detail-title" style={{ marginTop: 8 }}>
                  Paper positions ({d.positions.length})
                </div>
                {d.positions.map((pos) => (
                  <div key={pos.symbol} style={{ display: "flex", gap: 6, fontSize: 11 }}>
                    <span style={{ flex: "0 0 72px", fontWeight: 600 }}>{pos.symbol}</span>
                    <span className="num" style={{ flex: "0 0 84px", textAlign: "right" }}>
                      {usd(pos.market_value)}
                    </span>
                    <span
                      className="num"
                      style={{
                        flex: 1,
                        textAlign: "right",
                        color: (pos.unrealized_pl ?? 0) >= 0 ? "var(--green)" : "var(--red)",
                      }}
                    >
                      {pos.unrealized_pl != null ? `${pos.unrealized_pl >= 0 ? "+" : ""}${usd(pos.unrealized_pl)}` : "—"}
                    </span>
                  </div>
                ))}
              </>
            )}

            {!!d.recent_orders.length && (
              <>
                <div className="macro-detail-title" style={{ marginTop: 8 }}>
                  Recent orders
                </div>
                {d.recent_orders.slice(0, 6).map((o) => {
                  // If this name's position has since been fully closed, show
                  // "closed · +$pnl" (colored) instead of the raw "filled".
                  const sp = symbolPnl.get(o.symbol);
                  const closed =
                    (o.status ?? "") === "filled" && sp && sp.hasClosed && !sp.hasOpen;
                  return (
                    <div key={o.id} style={{ display: "flex", gap: 6, fontSize: 10, color: "var(--text-dim)" }}>
                      <span style={{ flex: "0 0 38px", color: SIDE_COLOR[o.side] }}>{o.side}</span>
                      <span style={{ flex: "0 0 72px" }}>{o.symbol}</span>
                      {closed ? (
                        <span
                          style={{ flex: "0 0 110px", color: (sp!.pnl) >= 0 ? "var(--green)" : "var(--red)" }}
                          title="position has been closed since this fill"
                        >
                          closed · {sp!.pnl >= 0 ? "+" : "−"}${Math.abs(sp!.pnl).toFixed(2)}
                        </span>
                      ) : (
                        <span style={{ flex: "0 0 70px", color: STATUS_COLOR[o.status ?? ""] ?? "var(--text-dim)" }}>
                          {o.status}
                        </span>
                      )}
                      <span className="num" style={{ flex: 1, textAlign: "right" }}>
                        {o.filled_avg_price ? `@ ${o.filled_avg_price}` : ""}
                        {o.error ? ` ${o.error.slice(0, 40)}` : ""}
                      </span>
                    </div>
                  );
                })}
              </>
            )}

            <DaySection />

            <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 8 }}>{d.disclaimer}</div>
          </>
        )}
      </div>
    </div>
  );
}
