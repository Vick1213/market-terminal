"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  DayReview,
  DayReviewFinding,
  DayReviewStatLeg,
  DayReviewSuggestion,
  SwingReview,
  Trade,
} from "@market/shared";
import { TradeChart } from "@/components/charts/TradeChart";
import {
  fetchDayReview,
  fetchSwingReview,
  fetchTrades,
  runDayReview,
  runSwingReview,
} from "@/lib/api";
import { useWebSocket } from "@/hooks/useWebSocket";

type Tab = "swing" | "day" | "learning";

const signed = (v: number | null | undefined) =>
  v === null || v === undefined
    ? "—"
    : `${v >= 0 ? "+" : "−"}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

const pnlColor = (v: number | null | undefined) =>
  v === null || v === undefined ? "var(--text-dim)" : v >= 0 ? "var(--green)" : "var(--red)";

function asDate(iso: string | null): Date | null {
  if (!iso) return null;
  const d = new Date(iso.includes("T") ? iso : iso.replace(" ", "T") + "Z");
  return Number.isNaN(d.getTime()) ? null : d;
}

// Compact clock for the row columns (e.g. "09:34"); "—" when absent.
function fmtClock(iso: string | null): string {
  const d = asDate(iso);
  return d ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—";
}

// Fuller stamp for the expanded detail (e.g. "Jun 29, 09:34").
function fmtFull(iso: string | null): string {
  const d = asDate(iso);
  return d
    ? d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : "—";
}

function fmtHold(mins: number | null): string {
  if (mins === null || mins === undefined) return "";
  if (mins < 60) return `${Math.round(mins)}m`;
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  return m ? `${h}h${m}m` : `${h}h`;
}

// One trade row: a compact column line + an inline-expanding detail (chart +
// full timing + full rationale). Expansion is a plain row toggle — no modal,
// no drag handler — so it behaves predictably like the Bot panel's rows.
function TradeRow({ t, intraday, open, onToggle }: {
  t: Trade;
  intraday: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const closed = t.status === "closed";
  // Chart lines: entry + the PLANNED protective levels recorded at entry (real
  // SL/TP from the bracket), falling back to the realized exit for TP when no
  // bracket target was stored.
  const levels =
    t.entry_price != null
      ? { entry: t.entry_price, tp: t.tp_price ?? t.exit_price ?? null, sl: t.sl_price ?? null }
      : null;
  const hasBracket = t.sl_price != null || t.tp_price != null || t.trail_price != null;
  return (
    <div style={{ borderBottom: "1px solid var(--border, #2a2a2a)", paddingBottom: 3, marginBottom: 3 }}>
      <div
        style={{ display: "flex", alignItems: "baseline", gap: 6, cursor: "pointer", fontSize: 12 }}
        onClick={onToggle}
      >
        <span style={{ minWidth: 10, color: "var(--text-dim)", fontSize: 10 }}>{open ? "▾" : "▸"}</span>
        <span className="num" style={{ flex: "0 0 40px", fontWeight: 600,
          color: t.direction === "short" ? "var(--red)" : "var(--green)" }}
          title={t.direction === "short" ? "short (sell→buy)" : "long (buy→sell)"}>
          {t.direction === "short" ? "SHORT" : "LONG"}
        </span>
        <span style={{ flex: "0 0 60px", fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis" }}>{t.symbol}</span>
        <span className="num" style={{ flex: "0 0 56px", textAlign: "right", color: "var(--text-dim)", fontSize: 10 }}
          title={`${t.qty} shares`}>
          {(t.qty ?? 0).toLocaleString(undefined, { maximumFractionDigits: 4 })}
        </span>
        <span className="num" style={{ flex: "0 0 92px", textAlign: "right", fontSize: 10, color: "var(--text-dim)" }}
          title="entry → exit price">
          {t.entry_price != null ? t.entry_price.toFixed(2) : "—"}
          {" → "}
          {t.exit_price != null ? t.exit_price.toFixed(2) : "·"}
        </span>
        {/* time columns: trade taken → trade close */}
        <span className="num" style={{ flex: "0 0 52px", textAlign: "right", fontSize: 10, color: "var(--text-dim)" }}
          title={`taken ${fmtFull(t.entry_time)}`}>
          {fmtClock(t.entry_time)}
        </span>
        <span className="num" style={{ flex: "0 0 52px", textAlign: "right", fontSize: 10, color: "var(--text-dim)" }}
          title={closed ? `closed ${fmtFull(t.exit_time)}` : "still open"}>
          {closed ? fmtClock(t.exit_time) : "—"}
        </span>
        <span style={{ flex: "0 0 96px", marginLeft: "auto", textAlign: "right", fontWeight: 600,
          color: closed ? pnlColor(t.pnl) : "var(--text-dim)" }}>
          {closed ? (
            <>
              closed · <span style={{ color: pnlColor(t.pnl) }}>{signed(t.pnl)}</span>
            </>
          ) : (
            <>
              open
              {t.pnl != null && (
                <span style={{ fontSize: 10, marginLeft: 4, color: pnlColor(t.pnl) }}>{signed(t.pnl)}</span>
              )}
            </>
          )}
        </span>
      </div>

      {/* planned protective levels (SL / TP / trail), always visible under the row */}
      {hasBracket && (
        <div style={{ paddingLeft: 16, fontSize: 10, display: "flex", gap: 10, flexWrap: "wrap" }}>
          {t.sl_price != null && (
            <span style={{ color: "var(--red)" }} title="stop-loss recorded at entry">
              SL {t.sl_price.toFixed(2)}
            </span>
          )}
          {t.tp_price != null && (
            <span style={{ color: "var(--green)" }} title="take-profit recorded at entry">
              TP {t.tp_price.toFixed(2)}
            </span>
          )}
          {t.trail_price != null && (
            <span style={{ color: "var(--yellow)" }} title="trailing-stop give-back ($) recorded at entry">
              trail ${t.trail_price.toFixed(2)}
            </span>
          )}
          {t.rr != null && (
            <span style={{ color: "var(--text-dim)" }} title="planned reward:risk">
              R:R {t.rr.toFixed(1)}
            </span>
          )}
        </div>
      )}

      {/* truncated rationale, always visible under the row */}
      {t.reason && (
        <div
          style={{ paddingLeft: 16, fontSize: 10, color: "var(--text-dim)", overflow: "hidden",
            textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          title={t.reason}
        >
          why: {t.reason}
        </div>
      )}

      {open && (
        <div style={{ padding: "3px 0 2px 16px", fontSize: 11, color: "var(--text-dim)" }}>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 2 }}>
            <span>taken {fmtFull(t.entry_time)}</span>
            <span>closed {closed ? fmtFull(t.exit_time) : "— (open)"}</span>
            {t.hold_minutes != null && <span>held {fmtHold(t.hold_minutes)}</span>}
            {closed && t.pnl_pct != null && (
              <span style={{ color: pnlColor(t.pnl) }}>
                {t.pnl_pct >= 0 ? "+" : ""}{t.pnl_pct.toFixed(2)}%
              </span>
            )}
            <span>{t.sleeve}</span>
          </div>
          {hasBracket && (
            <div style={{ marginBottom: 3 }}>
              <span style={{ color: "var(--accent, #7aa2f7)" }}>
                exit plan{t.exit_type ? ` (${t.exit_type})` : ""}:{" "}
              </span>
              {t.sl_price != null && <span style={{ color: "var(--red)" }}>stop {t.sl_price.toFixed(2)}</span>}
              {t.tp_price != null && <span style={{ color: "var(--green)" }}> · target {t.tp_price.toFixed(2)}</span>}
              {t.trail_price != null && <span style={{ color: "var(--yellow)" }}> · trail ${t.trail_price.toFixed(2)}</span>}
              {t.rr != null && <span style={{ color: "var(--text-dim)" }}> · planned R:R {t.rr.toFixed(1)}:1</span>}
            </div>
          )}
          {t.reason && (
            <div style={{ marginBottom: 3 }}>
              <span style={{ color: "var(--accent, #7aa2f7)" }}>why taken: </span>
              {t.reason}
            </div>
          )}
          <TradeChart symbol={t.symbol} intraday={intraday} levels={levels} />
        </div>
      )}
    </div>
  );
}

// Trade list for one sleeve — open trades first, then closed (newest first).
function TradeList({ sleeve }: { sleeve: "swing" | "day" }) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["trades", sleeve],
    queryFn: () => fetchTrades(sleeve),
    refetchInterval: 15_000,
  });
  const trades = data?.trades ?? [];
  const open = trades.filter((t) => t.status === "open");
  const closed = trades.filter((t) => t.status === "closed");

  if (isError) return <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>;
  if (isLoading) return <div style={{ color: "var(--text-dim)" }}>loading…</div>;
  if (!trades.length)
    return (
      <div style={{ color: "var(--text-dim)", fontSize: 11 }}>
        no {sleeve === "day" ? "short-term" : "long-term"} trades yet — they appear once the bot fills orders
      </div>
    );

  const intraday = sleeve === "day";
  const key = (t: Trade, i: number) => `${t.symbol}-${t.entry_time}-${t.exit_time ?? "open"}-${i}`;
  return (
    <>
      {!!open.length && <div className="macro-detail-title">Open ({open.length})</div>}
      {open.map((t, i) => {
        const k = `o-${key(t, i)}`;
        return <TradeRow key={k} t={t} intraday={intraday} open={openKey === k} onToggle={() => setOpenKey(openKey === k ? null : k)} />;
      })}
      {!!closed.length && (
        <div className="macro-detail-title" style={{ marginTop: open.length ? 8 : 0 }}>
          Closed ({closed.length})
        </div>
      )}
      {closed.map((t, i) => {
        const k = `c-${key(t, i)}`;
        return <TradeRow key={k} t={t} intraday={intraday} open={openKey === k} onToggle={() => setOpenKey(openKey === k ? null : k)} />;
      })}
    </>
  );
}

// --- Learning tab ---------------------------------------------------------

const SEV_COLOR: Record<string, string> = {
  critical: "var(--red)",
  warn: "var(--yellow)",
  info: "var(--text-dim)",
};

// A horizontal green win-rate bar (proportional plain div).
function WinBar({ label, leg }: { label: string; leg: DayReviewStatLeg }) {
  const wr = leg.win_rate;
  const pct = wr == null ? 0 : Math.round(wr * 100);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, marginBottom: 2 }}>
      <span style={{ flex: "0 0 88px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {label}
      </span>
      <span style={{ flex: 1, height: 8, background: "var(--border, #2a2a2a)", borderRadius: 3, overflow: "hidden" }}>
        <span style={{ display: "block", height: "100%", width: `${pct}%`, background: "var(--green)", opacity: 0.8 }} />
      </span>
      <span className="num" style={{ flex: "0 0 38px", textAlign: "right", color: "var(--text-dim)" }}>
        {wr == null ? "—" : `${pct}%`}
      </span>
      <span className="num" style={{ flex: "0 0 56px", textAlign: "right", color: pnlColor(leg.total_pnl) }}
        title={`${leg.n} trades · avg ${signed(leg.avg_pnl)}`}>
        {signed(leg.total_pnl)}
      </span>
    </div>
  );
}

function StatGroup({ title, legs }: { title: string; legs?: Record<string, DayReviewStatLeg> }) {
  const rows = Object.entries(legs ?? {})
    .filter(([k, v]) => k !== "none" && (v.wins + v.losses) > 0)
    .sort((a, b) => b[1].n - a[1].n);
  if (!rows.length) return null;
  return (
    <div style={{ marginBottom: 8 }}>
      <div className="macro-detail-title">{title}</div>
      {rows.map(([k, v]) => <WinBar key={k} label={k} leg={v} />)}
    </div>
  );
}

function FindingRow({ f }: { f: DayReviewFinding }) {
  const color = SEV_COLOR[f.severity] ?? "var(--text-dim)";
  return (
    <div style={{ display: "flex", gap: 6, fontSize: 11, marginBottom: 3, alignItems: "baseline" }}>
      <span style={{ flex: "0 0 4px", alignSelf: "stretch", background: color, borderRadius: 2 }} />
      <span style={{ minWidth: 0 }}>
        <span style={{ fontWeight: 600, color }}>{f.title}</span>
        <span style={{ color: "var(--text-dim)" }}> — {f.detail}</span>
      </span>
    </div>
  );
}

function SuggestionCard({ s }: { s: DayReviewSuggestion }) {
  // Advisory (low-sample / unvalidated) suggestions are de-emphasized so they're
  // never mistaken for a vetted, apply-it change.
  const advisory = s.actionable === false;
  const arrowColor = advisory ? "var(--text-dim)" : "var(--accent, #7aa2f7)";
  return (
    <div style={{ border: `1px solid ${advisory ? "var(--border, #2a2a2a)" : "var(--accent, #7aa2f7)"}`,
      borderRadius: 4, padding: "4px 6px", marginBottom: 4, fontSize: 11, opacity: advisory ? 0.7 : 1 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
        <span className="num" style={{ fontWeight: 600 }}>{s.param}</span>
        {s.confidence && (
          <span style={{ fontSize: 9, padding: "0 4px", borderRadius: 3, textTransform: "uppercase",
            color: advisory ? "var(--text-dim)" : "var(--green)",
            border: `1px solid ${advisory ? "var(--text-dim)" : "var(--green)"}` }}
            title={s.n != null ? `${s.n} closed trades` : undefined}>
            {advisory ? "advisory" : "ready"} · {s.confidence}{s.n != null ? ` · n=${s.n}` : ""}
          </span>
        )}
        <span className="num" style={{ marginLeft: "auto" }}>
          <span style={{ color: "var(--text-dim)" }}>{String(s.current)}</span>
          <span style={{ color: arrowColor }}> → {String(s.proposed)}</span>
        </span>
      </div>
      <div style={{ color: "var(--text-dim)", marginTop: 2 }}>{s.rationale}</div>
    </div>
  );
}

// Shared summary header + run button used by both review views.
function ReviewHeader({ label, model, runLabel, running, onRun }: {
  label: string;
  model?: string;
  runLabel: string;
  running: boolean;
  onRun: () => void;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
      <span style={{ fontSize: 10, color: "var(--text-dim)" }}>
        {label}
        {model ? ` · ${model}` : ""}
      </span>
      <button
        className="expand-btn"
        style={{ marginLeft: "auto", fontSize: 11 }}
        disabled={running}
        title={runLabel}
        onClick={onRun}
      >
        {running ? "running…" : "↻ Run review now"}
      </button>
    </div>
  );
}

function DayLearningView() {
  const qc = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["day-review"],
    queryFn: fetchDayReview,
    refetchInterval: 15_000,
  });
  const run = useMutation({
    mutationFn: runDayReview,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["day-review"] }),
  });

  const review = (data ?? {}) as DayReview;
  const empty = !review || Object.keys(review).length === 0 || review.no_data;
  const stats = review.stats;
  const findings = (review.findings ?? []).filter((f) => f.tag !== "none");
  const suggestions = review.suggestions ?? [];

  if (isError) return <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>;

  return (
    <div>
      <ReviewHeader
        label={review.trade_date ? `session ${review.trade_date}` : "day learning loop"}
        model={review.model}
        runLabel="Run the end-of-day review now (backfills P&L + LLM/heuristic findings)"
        running={run.isPending}
        onRun={() => run.mutate()}
      />

      {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}

      {!isLoading && empty && (
        <div style={{ color: "var(--text-dim)", fontSize: 11 }}>
          no review yet — runs automatically after market close, or hit “Run review now”.
        </div>
      )}

      {!isLoading && !empty && (
        <>
          {review.summary && (
            <div style={{ fontSize: 12, lineHeight: 1.45, marginBottom: 8,
              padding: "5px 7px", border: "1px solid var(--border, #2a2a2a)", borderRadius: 4 }}>
              {review.summary}
            </div>
          )}

          <StatGroup title="Win-rate by setup" legs={stats?.by_signal_kind} />
          <StatGroup title="Long vs short" legs={stats?.by_direction} />
          <StatGroup title="Win-rate by regime" legs={stats?.by_regime} />
          <StatGroup title="Win-rate by symbol" legs={stats?.by_symbol} />

          {!!findings.length && (
            <div style={{ marginBottom: 8 }}>
              <div className="macro-detail-title">Findings</div>
              {findings.map((f, i) => <FindingRow key={i} f={f} />)}
            </div>
          )}

          {!!suggestions.length && (
            <div>
              <div className="macro-detail-title">Suggested parameter tweaks</div>
              {suggestions.map((s, i) => <SuggestionCard key={i} s={s} />)}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// One realized-vs-marked summary line for the swing book.
function SwingOverall({ stats }: { stats: SwingReview["stats"] }) {
  const o = stats?.overall;
  if (!o) return null;
  return (
    <div style={{ fontSize: 11, marginBottom: 8, display: "flex", gap: 12, flexWrap: "wrap" }}>
      <span>
        <span style={{ color: "var(--text-dim)" }}>closed </span>
        {stats?.n_closed ?? 0}
        {o.win_rate != null && (
          <span style={{ color: "var(--text-dim)" }}> · {Math.round(o.win_rate * 100)}% win</span>
        )}
        <span style={{ color: pnlColor(o.total_pnl), marginLeft: 6 }}>{signed(o.total_pnl)}</span>
      </span>
      {!!stats?.n_open && (
        <span>
          <span style={{ color: "var(--text-dim)" }}>open </span>
          {stats.n_open}
          {stats.open_pnl != null && (
            <span style={{ color: pnlColor(stats.open_pnl), marginLeft: 6 }}>
              {signed(stats.open_pnl)}
            </span>
          )}
        </span>
      )}
    </div>
  );
}

function SwingLearningView() {
  const qc = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["swing-review"],
    queryFn: fetchSwingReview,
    refetchInterval: 60_000,
  });
  const run = useMutation({
    mutationFn: runSwingReview,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["swing-review"] }),
  });

  const review = (data ?? {}) as SwingReview;
  const empty = !review || Object.keys(review).length === 0 || review.no_data;
  const stats = review.stats;
  const findings = (review.findings ?? []).filter((f) => f.tag !== "none");
  const suggestions = review.suggestions ?? [];

  if (isError) return <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>;

  return (
    <div>
      <ReviewHeader
        label={review.review_date ? `reviewed ${review.review_date}` : "swing learning loop"}
        model={review.model}
        runLabel="Run the swing review now (mines the swing tradebook)"
        running={run.isPending}
        onRun={() => run.mutate()}
      />

      {isLoading && <div style={{ color: "var(--text-dim)" }}>loading…</div>}

      {!isLoading && empty && (
        <div style={{ color: "var(--text-dim)", fontSize: 11 }}>
          no swing review yet — runs weekly after Friday’s close, or hit “Run review now”.
        </div>
      )}

      {!isLoading && !empty && (
        <>
          {review.summary && (
            <div style={{ fontSize: 12, lineHeight: 1.45, marginBottom: 8,
              padding: "5px 7px", border: "1px solid var(--border, #2a2a2a)", borderRadius: 4 }}>
              {review.summary}
            </div>
          )}

          <SwingOverall stats={stats} />

          <StatGroup title="Win-rate by bucket" legs={stats?.by_bucket} />
          <StatGroup title="Long vs short" legs={stats?.by_direction} />
          <StatGroup title="Win-rate by symbol" legs={stats?.by_symbol} />

          {!!findings.length && (
            <div style={{ marginBottom: 8 }}>
              <div className="macro-detail-title">Findings</div>
              {findings.map((f, i) => <FindingRow key={i} f={f} />)}
            </div>
          )}

          {!!suggestions.length && (
            <div>
              <div className="macro-detail-title">Suggested parameter tweaks</div>
              {suggestions.map((s, i) => <SuggestionCard key={i} s={s} />)}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// Learning tab: a sub-toggle between the fast DAY loop and the slow SWING loop.
function LearningTab() {
  const [mode, setMode] = useState<"day" | "swing">("swing");
  const MODES: { id: "day" | "swing"; label: string }[] = [
    { id: "swing", label: "Long term" },
    { id: "day", label: "Short term" },
  ];
  return (
    <div>
      <div className="chip-row" style={{ marginBottom: 6 }}>
        {MODES.map((m) => (
          <button
            key={m.id}
            className={`chip ${mode === m.id ? "active" : ""}`}
            onClick={() => setMode(m.id)}
          >
            {m.label}
          </button>
        ))}
      </div>
      {mode === "swing" ? <SwingLearningView /> : <DayLearningView />}
    </div>
  );
}

// --- Panel shell ----------------------------------------------------------

export function TradebookPanel() {
  const [tab, setTab] = useState<Tab>("swing");
  const qc = useQueryClient();

  // Live-refresh on any bot WS event (fills/exits/reviews), like BotPanel.
  const { status, last } = useWebSocket("bot", 1);
  useEffect(() => {
    if (last) {
      qc.invalidateQueries({ queryKey: ["trades"] });
      qc.invalidateQueries({ queryKey: ["day-review"] });
      qc.invalidateQueries({ queryKey: ["swing-review"] });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [last]);

  const TABS: { id: Tab; label: string }[] = [
    { id: "swing", label: "Long term" },
    { id: "day", label: "Short term" },
    { id: "learning", label: "Learning" },
  ];

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Tradebook</span>
        <span className={`badge ${status === "open" ? "live" : "closed"}`}>
          {status === "open" ? "WS live" : status}
        </span>
      </div>
      <div className="panel-body">
        <div className="chip-row" style={{ marginBottom: 6 }}>
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`chip ${tab === t.id ? "active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
        {tab === "swing" && <TradeList sleeve="swing" />}
        {tab === "day" && <TradeList sleeve="day" />}
        {tab === "learning" && <LearningTab />}
      </div>
    </div>
  );
}
