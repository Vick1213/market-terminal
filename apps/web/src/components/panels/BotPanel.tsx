"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { BotProposal } from "@market/shared";
import {
  executeBotProposal,
  fetchBotStatus,
  reconcileBot,
  runBotPropose,
  setBotEnabled,
  setBotMode,
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

function ProposalRow({
  p,
  canExecute,
  open,
  onToggle,
  onExecute,
  executing,
}: {
  p: BotProposal;
  canExecute: boolean;
  open: boolean;
  onToggle: () => void;
  onExecute: () => void;
  executing: boolean;
}) {
  const size = p.side === "buy" ? usd(p.notional) : `${(p.qty ?? 0).toLocaleString(undefined, { maximumFractionDigits: 4 })} sh`;
  const actionable = p.status === "proposed";
  return (
    <div style={{ marginBottom: 3, borderBottom: "1px solid var(--border, #2a2a2a)", paddingBottom: 3 }}>
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
        <span
          style={{
            marginLeft: "auto",
            fontSize: 10,
            textTransform: "uppercase",
            color: STATUS_COLOR[p.status] ?? "var(--text-dim)",
          }}
        >
          {p.status}
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
        </div>
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
  const execute = useMutation({
    mutationFn: executeBotProposal,
    onSuccess: (r) => {
      invalidate();
      if (r && r.ok === false) window.alert(`Not executed: ${r.detail ?? "blocked"}`);
    },
  });

  const [openId, setOpenId] = useState<number | null>(null);
  const d = q.data;

  const brokerOff = d && !d.broker.enabled;
  const enabled = !!d?.config.enabled;
  const mode = d?.config.mode ?? "proposal";
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
                {d.recent_orders.slice(0, 6).map((o) => (
                  <div key={o.id} style={{ display: "flex", gap: 6, fontSize: 10, color: "var(--text-dim)" }}>
                    <span style={{ flex: "0 0 38px", color: SIDE_COLOR[o.side] }}>{o.side}</span>
                    <span style={{ flex: "0 0 72px" }}>{o.symbol}</span>
                    <span style={{ flex: "0 0 70px", color: STATUS_COLOR[o.status ?? ""] ?? "var(--text-dim)" }}>
                      {o.status}
                    </span>
                    <span className="num" style={{ flex: 1, textAlign: "right" }}>
                      {o.filled_avg_price ? `@ ${o.filled_avg_price}` : ""}
                      {o.error ? ` ${o.error.slice(0, 40)}` : ""}
                    </span>
                  </div>
                ))}
              </>
            )}

            <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 8 }}>{d.disclaimer}</div>
          </>
        )}
      </div>
    </div>
  );
}
