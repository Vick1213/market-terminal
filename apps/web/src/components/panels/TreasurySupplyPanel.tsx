"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { TreasuryAuction } from "@market/shared";
import { fetchTreasuryAuctions, runTreasuryAuctions } from "@/lib/api";

// High dealer takedown = real money stayed away (yield-spike risk); a low
// bid-to-cover is weak demand. Colour the dealer % hot when dealers are stuck
// with a big share of the auction.
function dealerColor(pct: number | null): string {
  if (pct == null) return "var(--text-dim)";
  if (pct >= 25) return "var(--red)";
  if (pct >= 17) return "var(--yellow)";
  return "var(--green)";
}

function btcColor(btc: number | null): string {
  if (btc == null) return "var(--text-dim)";
  if (btc >= 2.5) return "var(--green)";
  if (btc >= 2.2) return "var(--yellow)";
  return "var(--red)";
}

function fmt(n: number | null, digits = 2): string {
  return n == null ? "—" : n.toFixed(digits);
}

function fmtPct(n: number | null): string {
  return n == null ? "—" : `${n.toFixed(1)}%`;
}

function AuctionRow({ a }: { a: TreasuryAuction }) {
  const title = [
    `${a.security_type} ${a.security_term} — auctioned ${a.auction_date}`,
    a.high_yield != null ? `high yield ${a.high_yield}%` : null,
    a.offering_amt != null ? `offering $${(a.offering_amt / 1e9).toFixed(1)}B` : null,
    `dealer ${fmtPct(a.dealer_pct)} · indirect ${fmtPct(a.indirect_pct)} · direct ${fmtPct(a.direct_pct)}`,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <tr className="wl-row" title={title}>
      <td>
        <span className="wl-sym">{a.security_term}</span>
      </td>
      <td style={{ color: "var(--text-dim)" }}>{a.security_type}</td>
      <td className="num" style={{ color: btcColor(a.bid_to_cover) }}>
        {fmt(a.bid_to_cover)}
      </td>
      <td className="num">{fmt(a.high_yield, 3)}</td>
      <td className="num" style={{ color: dealerColor(a.dealer_pct) }}>
        {fmtPct(a.dealer_pct)}
      </td>
      <td className="num" style={{ color: "var(--text-dim)" }}>
        {fmtPct(a.indirect_pct)}
      </td>
      <td className="num" style={{ color: "var(--text-dim)" }}>
        {a.auction_date.slice(5)}
      </td>
    </tr>
  );
}

export function TreasurySupplyPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["treasury-auctions"],
    queryFn: () => fetchTreasuryAuctions(40),
    refetchInterval: 30 * 60_000,
  });

  const refresh = useMutation({
    mutationFn: runTreasuryAuctions,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["treasury-auctions"] }),
  });

  const auctions = q.data?.auctions ?? [];

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Treasury Supply — Auction Demand</span>
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          style={{ fontSize: 10 }}
          title="Pull the latest Treasury auction results now"
        >
          {refresh.isPending ? "…" : "refresh"}
        </button>
      </div>
      <div className="panel-body">
        {q.isError && (
          <div style={{ color: "var(--red)" }}>API unreachable — is the backend running?</div>
        )}
        {!q.isError && !auctions.length && (
          <div style={{ color: "var(--text-dim)" }}>no recent auctions — refreshes every 12h</div>
        )}

        {!!auctions.length && (
          <table className="wl-table">
            <thead>
              <tr>
                <th>term</th>
                <th>type</th>
                <th className="num">b/c</th>
                <th className="num">yield</th>
                <th className="num">dealer</th>
                <th className="num">indir</th>
                <th className="num">date</th>
              </tr>
            </thead>
            <tbody>
              {auctions.map((a, i) => (
                <AuctionRow key={`${a.auction_date}-${a.security_term}-${i}`} a={a} />
              ))}
            </tbody>
          </table>
        )}

        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 8 }}>
          b/c = bid-to-cover (higher = stronger demand). dealer % high (red) = primary dealers stuck
          with the auction → real money stayed away, yield-spike risk. Bills quote a discount rate so
          high-yield is blank.
        </div>
      </div>
    </div>
  );
}
