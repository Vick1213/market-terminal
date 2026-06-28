"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { KalshiBucket, KalshiEvent } from "@market/shared";
import { fetchKalshi, runKalshi } from "@/lib/api";

const SERIES_LABEL: Record<string, string> = {
  KXFED: "FOMC target rate",
  KXCPI: "CPI print",
};

function pct(p: number): string {
  return `${(p * 100).toFixed(0)}%`;
}

function BucketBar({ b, modal }: { b: KalshiBucket; modal: boolean }) {
  const w = Math.max(0, Math.min(100, b.bucket_prob * 100));
  return (
    <div
      style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 10, lineHeight: "16px" }}
      title={`${b.label} — implied ${pct(b.bucket_prob)} (cumulative above ${pct(b.cum_prob ?? 0)})`}
    >
      <span
        style={{
          width: 64,
          textAlign: "right",
          color: modal ? "var(--green)" : "var(--text-dim)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {b.label}
      </span>
      <div style={{ flex: 1, background: "var(--bg-2, #1a1a1a)", borderRadius: 2, height: 10 }}>
        <div
          style={{
            width: `${w}%`,
            height: "100%",
            borderRadius: 2,
            background: modal ? "var(--green)" : "var(--yellow)",
            opacity: modal ? 1 : 0.6,
          }}
        />
      </div>
      <span className="num" style={{ width: 34, color: modal ? "var(--green)" : "var(--text-dim)" }}>
        {pct(b.bucket_prob)}
      </span>
    </div>
  );
}

function EventBlock({ e }: { e: KalshiEvent }) {
  // Only show buckets carrying meaningful mass to keep the ladder readable.
  const shown = e.strikes.filter((b) => b.bucket_prob >= 0.01);
  return (
    <div style={{ marginBottom: 10 }}>
      <div className="macro-detail-title" style={{ display: "flex", justifyContent: "space-between" }}>
        <span>{SERIES_LABEL[e.series_ticker] ?? e.series_ticker}</span>
        <span style={{ color: "var(--text-dim)", fontWeight: 400 }}>
          {e.close_time ? `closes ${e.close_time}` : ""}
        </span>
      </div>
      {e.modal && (
        <div style={{ fontSize: 10, color: "var(--green)", marginBottom: 4 }}>
          modal: {e.modal.label} @ {pct(e.modal.bucket_prob)}
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {shown.map((b, i) => (
          <BucketBar key={`${b.floor_strike ?? b.label}-${i}`} b={b} modal={b === e.modal} />
        ))}
      </div>
    </div>
  );
}

export function KalshiPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["kalshi"],
    queryFn: fetchKalshi,
    refetchInterval: 15 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: runKalshi,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["kalshi"] }),
  });

  const events = q.data?.events ?? [];
  const fomc = q.data?.fomc_expected_rate ?? null;

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Kalshi — Rate &amp; CPI Odds</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          style={{ fontSize: 10 }}
          title="Pull the latest Kalshi FOMC/CPI market snapshot now"
        >
          {refresh.isPending ? "…" : "refresh"}
        </button>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && !events.length && (
          <div style={{ color: "var(--text-dim)" }}>
            no Kalshi markets — enable the ingest in config to populate
          </div>
        )}

        {fomc && (
          <div style={{ fontSize: 11, marginBottom: 8 }}>
            implied FOMC rate:{" "}
            <span className="num" style={{ color: "var(--green)" }}>
              {fomc.expected_rate.toFixed(2)}%
            </span>{" "}
            <span style={{ color: "var(--text-dim)", fontSize: 9 }}>as of {fomc.ts}</span>
          </div>
        )}

        {events.map((e) => (
          <EventBlock key={e.event_ticker} e={e} />
        ))}

        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 4 }}>
          Probabilities differenced from Kalshi&apos;s cumulative &ldquo;Above X&rdquo; ladder into
          per-bucket implied mass. Implied FOMC rate is the probability-weighted target.
        </div>
      </div>
    </div>
  );
}
