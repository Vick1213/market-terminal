"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

/* Static reference content — how to read and act on every panel. Kept in one
   data structure so the compact accordion and the expanded modal share it. */

function Term({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 6 }}>
      <span style={{ color: "var(--text, #ddd)", fontWeight: 600 }}>{k}</span>{" "}
      <span style={{ color: "var(--text-dim)" }}>{children}</span>
    </div>
  );
}

function P({ children }: { children: React.ReactNode }) {
  return <p style={{ margin: "0 0 8px", color: "var(--text-dim)" }}>{children}</p>;
}

function Use({ children }: { children: React.ReactNode }) {
  return (
    <p style={{ margin: "0 0 8px", color: "var(--text, #ddd)" }}>
      <span style={{ color: "var(--green)", fontWeight: 600 }}>How to use it: </span>
      {children}
    </p>
  );
}

const SECTIONS: { id: string; title: string; body: React.ReactNode }[] = [
  {
    id: "basics",
    title: "Reading the terminal (start here)",
    body: (
      <>
        <P>
          Every panel is a different lens on the same question: <em>is money moving toward
          risk or away from it, and is anything breaking?</em> Panels drag by their title
          bar and most expand (⤢ or click) into a detail view.
        </P>
        <Term k="z-score (z)">
          how unusual a value is vs its own recent history, in standard deviations.
          z = 0 is normal, ±1 is mildly stretched, ±2 is genuinely unusual (~5% of days),
          ±3+ is extreme. Most alerts and statuses here are z-score driven.
        </Term>
        <Term k="Regime">
          the macro composite&apos;s one-word summary — RISK-ON / NEUTRAL / RISK-OFF /
          STRESS. It gates how to read everything else: a signal that means
          &quot;buy the dip&quot; in risk-on often means &quot;get out of the way&quot; in stress.
        </Term>
        <Term k="Freshness badges">
          crypto is genuinely live (WS); equities/macro are delayed or daily; COT is
          weekly; 13F-type data lags weeks. Badges and tooltips state each datum&apos;s
          true age — trust the badge, not the vibe.
        </Term>
        <Term k="WS live">
          green means the panel has a live WebSocket feed and updates push in without
          polling. If it shows &quot;closed&quot;, the backend is down or restarting.
        </Term>
      </>
    ),
  },
  {
    id: "brief",
    title: "Morning Brief",
    body: (
      <>
        <P>
          A pre-market synthesis of everything below: regime, last-24h alerts, broken
          correlations, retail spikes, insider buys, COT extremes, SPX gamma, and the
          week&apos;s events, plus net liquidity, congress watchlist trades and new whale
          positions. Written by the configured LLM (chip shows provider:model — local
          Ollama by default, or OpenAI / DeepSeek / Anthropic via MARKET_LLM_PROVIDER +
          MARKET_LLM_API_KEY); if the chip says <em>template</em>, the LLM was
          unreachable and you get the same facts as plain bullet points.
        </P>
        <Use>
          read it once with coffee instead of scanning 11 panels. ↻ regenerates on
          demand (e.g. after a big overnight move). Nothing in it is invented — every
          line is grounded in stored data.
        </Use>
      </>
    ),
  },
  {
    id: "alerts",
    title: "Alerts",
    body: (
      <>
        <P>
          A sweep every 10 minutes over already-stored data. Rules fire on the
          <em> crossing</em> into an unusual state (not continuously) and each has a
          24h cooldown, so the feed stays quiet enough to matter. Click a row for the
          full explanation.
        </P>
        <Term k="regime flip ◑">the composite changed regime — re-check sizing and any reversion trades.</Term>
        <Term k="macro z σ">VIX, credit spreads, put/call, financial conditions or the dollar moved 2σ+ vs its 6-month range.</Term>
        <Term k="vix term 〽">VIX3M/VIX crossed below 1.0 (backwardation) — the classic early risk-off tripwire.</Term>
        <Term k="corr break ⛓">a Cookbook card flipped to BROKEN. In stress: structural, don&apos;t fade. Otherwise: possible reversion setup.</Term>
        <Term k="retail spike 📈">a ticker&apos;s mentions ran 3x+ its 24h-ago count — check the Retail drill-down for whether the crowd is bullish or bearish.</Term>
        <Term k="insider 👤 / COT ⚖ / gamma Γ / whale 🐋">see Smart Money below.</Term>
        <P>
          The <em>ntfy</em> chip: set MARKET_NTFY_TOPIC in apps/api/.env (any long random
          string), install the ntfy app on your phone and subscribe to the same topic —
          warn/critical alerts then push to you. Off = alerts stay in this panel.
        </P>
      </>
    ),
  },
  {
    id: "calendar",
    title: "Event Horizon",
    body: (
      <>
        <P>
          The forward schedule of things that move markets: FOMC decisions, CPI / NFP /
          PPI / GDP / PCE prints, monthly OPEX, quad witching, futures roll week, COT
          report Fridays, and your watchlist&apos;s earnings dates.
        </P>
        <Term k="FOMC">Fed rate decision, 14:00 ET + press conference. The single biggest scheduled vol event.</Term>
        <Term k="CPI / NFP / PCE">inflation, jobs, and the Fed&apos;s preferred inflation gauge — all 08:30 ET, all capable of repricing the whole curve in minutes.</Term>
        <Term k="OPEX / quad witching">monthly options expiry (3rd Friday). Quad witching (Mar/Jun/Sep/Dec) adds index futures+options — dealer hedging unwinds, so support/resistance from gamma walls weakens into and after it.</Term>
        <Term k="~est">date estimated by rule of thumb, not from an official schedule.</Term>
        <Use>
          before acting on any other signal, glance here. A correlation break two days
          before FOMC is usually positioning, not regime change. Red TODAY/tomorrow
          chips = expect noise around 08:30/14:00 ET.
        </Use>
      </>
    ),
  },
  {
    id: "news",
    title: "News + Sentiment",
    body: (
      <>
        <P>
          Headlines from Yahoo per-ticker RSS, SEC EDGAR, broad finance feeds and GDELT,
          deduped across outlets and scored −1..+1 by a local FinBERT model. The score
          color is the model&apos;s read of the <em>headline</em>, not the market&apos;s reaction.
        </P>
        <Term k="multi-outlet badge">the same story arrived from N independent outlets — convergence makes it more likely to matter.</Term>
        <Term k="EDGAR chips">8-K item codes; 1.05 (cyber incident), 2.02 (results), 8.01 (other material) are the ones that gap stocks.</Term>
        <Term k="+ticker">type a symbol in the chip row to track it in every per-symbol news source (Yahoo/EDGAR/Finnhub/SeekingAlpha) without putting it on the watchlist; × on its chip stops tracking and clears its items.</Term>
        <Use>
          treat single-outlet, low-confidence scores as noise. What matters: clusters of
          same-direction headlines on one name, or an 8-K chip on a watchlist ticker.
        </Use>
      </>
    ),
  },
  {
    id: "retail",
    title: "Retail Market Score",
    body: (
      <>
        <P>
          What the retail crowd is piling into, from ApeWisdom mention counts
          (Reddit/WSB) cross-read with FinBERT-scored StockTwits/Bluesky text. The gauge
          is crowd bull/bear; the table is the spike leaderboard.
        </P>
        <Term k="z">mention-volume z-score — how abnormal today&apos;s chatter is for that ticker.</Term>
        <Term k="rank Δ">rank velocity vs 24h ago — fast climbers matter more than big-but-stable names.</Term>
        <Term k="⚠ divergence">volume spiking while the text is bearish — crowds also pile into disasters; don&apos;t assume a spike is bullish.</Term>
        <Term k="conf dots">how many independent sources confirm the read — one source is rumor-grade.</Term>
        <Use>
          this is a contrarian/early-warning tool, not a buy list. A new name entering
          the top with high z and multi-source confirmation is worth understanding
          <em> before</em> it&apos;s on CNBC; extreme euphoria on a name you hold is a risk flag.
        </Use>
      </>
    ),
  },
  {
    id: "macro",
    title: "Liquidity & Macro (the regime gauge)",
    body: (
      <>
        <P>
          A composite −100..+100 Risk-On/Off score built from volatility (VIX complex),
          credit spreads, financial conditions, Fed net liquidity, breadth, and
          sentiment surveys (NAAIM/AAII), each as a z-score, weighted into buckets. The
          regime word (risk-on / neutral / risk-off / stress) comes from this.
        </P>
        <Term k="Net liquidity">Fed balance sheet − reverse repo − Treasury account: the &quot;money available to markets&quot; proxy that has led SPY/BTC for years.</Term>
        <Term k="NET LIQ strip">
          the daily version of that number (in $tn) — TGA from Treasury&apos;s daily
          statement and ON RRP from the NY Fed, not the weekly FRED prints. RISING is
          a tailwind for risk with a ~5-6 week lead; the 1w/1m deltas show whether
          liquidity is being added or drained <em>right now</em>. Chart it from any
          chart picker as &quot;Fed net liquidity ($bn, daily)&quot;.
        </Term>
        <Term k="Contrarian extremes">NAAIM bottom decile + AAII bears ≫ bulls + elevated put/call together mark capitulation — historically a better buy signal than any headline.</Term>
        <Use>
          expand it to see <em>which bucket</em> drives the score. A risk-off print driven
          by vol alone fades fast; one driven by credit + liquidity together is the
          kind to respect.
        </Use>
      </>
    ),
  },
  {
    id: "watchlist",
    title: "Watchlist & Multi-Asset",
    body: (
      <>
        <P>
          <strong>Watchlist:</strong> your symbols with quotes, sentiment and spark
          context; add/remove feeds everything else (insider tracking, earnings dates,
          seasonality all follow the watchlist).
        </P>
        <P>
          <strong>Multi-Asset:</strong> the live tape — streaming BTC/ETH prints with
          large-trade flagging (notional or z-score), order-book depth, metals spot,
          FINRA short volume and crypto aggregates. Crypto here is genuinely real-time;
          it&apos;s the canary that trades 24/7 while equities sleep.
        </P>
        <Use>
          weekend/overnight risk shows up in BTC first. A cascade of large sells plus
          thinning depth before Monday open is information equities can&apos;t give you yet.
        </Use>
      </>
    ),
  },
  {
    id: "rotation",
    title: "Sector Rotation (RRG vs SPY) — the scatter explained",
    body: (
      <>
        <P>
          Each dot is one S&amp;P 500 <em>sector ETF</em>, plotted by how it&apos;s performing
          <em> relative to SPY</em> (the S&amp;P 500 itself), not in absolute terms:
        </P>
        <Term k="XLK">Technology (Apple, Microsoft, NVIDIA)</Term>
        <Term k="XLF">Financials (banks, brokers, insurers)</Term>
        <Term k="XLE">Energy (oil &amp; gas)</Term>
        <Term k="XLV">Health Care</Term>
        <Term k="XLI">Industrials</Term>
        <Term k="XLP">Consumer Staples (food, household — defensive)</Term>
        <Term k="XLY">Consumer Discretionary (retail, autos — cyclical)</Term>
        <Term k="XLU">Utilities (the classic defensive/bond-proxy sector)</Term>
        <Term k="XLB">Materials (chemicals, miners)</Term>
        <Term k="XLRE">Real Estate (REITs)</Term>
        <Term k="XLC">Communication Services (Google, Meta, telecom)</Term>
        <P>
          <strong>X-axis (RS-Ratio):</strong> trend of relative strength — right of
          center = outperforming SPY this quarter. <strong>Y-axis (RS-Momentum):</strong>{" "}
          whether that relative performance is accelerating (top) or fading (bottom).
          The faint trail = the last 8 weeks, so you see the direction of travel.
        </P>
        <Term k="LEADING (green, top-right)">outperforming and still accelerating — where money currently is.</Term>
        <Term k="WEAKENING (yellow, bottom-right)">still ahead of SPY but losing steam — often the next source of selling.</Term>
        <Term k="LAGGING (red, bottom-left)">underperforming and getting worse — avoid, or hunt for capitulation.</Term>
        <Term k="IMPROVING (blue, top-left)">still behind SPY but accelerating — where money is <em>starting</em> to go; the classic early-entry quadrant.</Term>
        <P>
          Sectors tend to rotate clockwise: improving → leading → weakening → lagging.
          The <em>character</em> of leadership is the regime tell: XLU/XLP/XLV leading =
          defensive market under the surface even if indexes are flat; XLK/XLY leading =
          genuine risk appetite.
        </P>
        <P>
          <strong>Seasonality table:</strong> for each watchlist symbol, the historical
          average return and win rate for the current and next calendar month
          (&quot;+3.7% · 100%&quot; = averaged +3.7% and rose in every sampled year). A tilt,
          never a trigger — with ~5 years of data a 75% win rate is 3 of 4 years.
        </P>
      </>
    ),
  },
  {
    id: "smartmoney",
    title: "Smart Money — COT · Gamma · Insiders",
    body: (
      <>
        <P>
          <strong>CFTC positioning (COT):</strong> every Friday the CFTC reports what
          large speculators (hedge funds, CTAs — &quot;non-commercials&quot;) hold in futures.
          &quot;Net spec&quot; = their longs minus shorts. The <em>COT index</em> rescales today&apos;s
          net onto its 3-year range: 0 = most bearish positioning in 3y, 100 = most
          bullish. 13w Δ shows the recent direction of travel.
        </P>
        <Use>
          extremes (≥90 or ≤10, red) mark <em>crowded</em> trades — everyone who wanted in
          is in, so the path of least resistance is an unwind. It&apos;s a fade/timing
          overlay with weeks-long horizon, not a same-day signal. Data is as-of Tuesday,
          published Friday.
        </Use>
        <P>
          <strong>Gamma (GEX) strip:</strong> computed from the SPX/SPY options chain.
          Dealers who sold options must hedge with the underlying; how much and in which
          direction depends on net gamma.
        </P>
        <Term k="Γ LONG (green)">dealers buy dips and sell rips — moves get dampened, ranges compress, walls act as magnets/pins.</Term>
        <Term k="Γ SHORT (red)">dealers sell into weakness and buy into strength — moves get amplified. Expect wider ranges and faster tails.</Term>
        <Term k="flip">the index level where the regime flips between the two. Spot below flip = short-gamma (unstable) territory.</Term>
        <Term k="call wall / put wall">the strikes with the most dealer gamma — practical resistance / support, strongest into OPEX (then they reset).</Term>
        <P>
          <strong>Form 4 insider buys:</strong> officers and directors must file when
          they trade their own stock. Only <em>open-market purchases</em> (code P) count
          here — sells are routine (compensation), buys are conviction with personal
          money. ⚑ = cluster (2+ distinct insiders buying within the window), ★ =
          CEO/CFO among them — the two patterns with documented forward outperformance,
          especially after a drawdown.
        </P>
        <Use>
          the three blocks answer one question from three angles: futures money
          (weeks), options dealers (days), and the people who know the company
          (months). Agreement across them is rare and worth acting on.
        </Use>
      </>
    ),
  },
  {
    id: "whales",
    title: "Whales — 13F Funds · Congress",
    body: (
      <>
        <P>
          <strong>13F whale portfolios:</strong> famous funds (Berkshire, Bridgewater,
          Pershing Square, Scion, Druckenmiller&apos;s Duquesne, ...) must disclose US
          equity holdings quarterly, within 45 days. Each row is a fund; click it for
          the top holdings with quarter-over-quarter share-count changes. NEW marks a
          position that entered the top-20; new/exit counts summarize turnover.
        </P>
        <Use>
          this is a <em>conviction</em> feed, not a timing feed — the data is up to 45
          days stale and funds can hedge invisibly. What matters: several whales
          initiating the same name, a famous bear (Scion) going long, or a fund
          concentrating into fewer names. Ignore small trims.
        </Use>
        <P>
          <strong>Senate stock trades (PTRs):</strong> senators must disclose personal
          trades within 45 days, in dollar bands. Scraped from the official Senate eFD
          site — electronic filings only (paper filings are scanned images; House
          disclosures are PDF-only and not tracked). ⚑ = the ticker is on your
          watchlist.
        </P>
        <Use>
          single trades are noise (spouses, advisers, index funds). The signal is
          clustering: several senators buying the same sector ahead of legislation, or
          committee members trading their own subject matter. Treat as a curiosity
          screen that occasionally surfaces something worth researching.
        </Use>
      </>
    ),
  },
  {
    id: "cookbook",
    title: "Correlation Cookbook",
    body: (
      <>
        <P>
          12 textbook intermarket relationships (gold vs real yields, copper/gold vs
          10y, net liquidity vs BTC, VIX term structure, yield curve, ...) each checked
          continuously against its historical behavior.
        </P>
        <Term k="HOLDS">the relationship is behaving — nothing to do.</Term>
        <Term k="WATCH">drifting 1-2σ from normal — on the radar.</Term>
        <Term k="BROKEN">2σ+ divergence — either a tradable mean-reversion or the start of a structural change.</Term>
        <Use>
          the regime banner is the gate: in risk-on/neutral, a BROKEN mean-reverting
          pair is a candidate to fade (expect re-convergence). In stress, breaks are
          how new regimes announce themselves — do not fade them. Expand a card for
          rolling correlation history, rebased legs and lead-lag.
        </Use>
      </>
    ),
  },
  {
    id: "strategist",
    title: "Strategist (signal synthesis)",
    body: (
      <>
        <P>
          A daily cross-panel synthesis: the macro regime sets a base allocation across
          the terminal&apos;s asset classes (equities / gold &amp; silver / BTC &amp; ETH /
          cash), then every stored signal — net-liquidity trend, dealer gamma, stress
          radar, cookbook breaks, COT crowding, put/call extremes, the 48h news pulse,
          macro-event proximity (FOMC/CPI/NFP), crypto large-print flow, benchmark
          seasonality, retail froth, insider clusters (watchlist + market-wide scan),
          whale new positions and 10-K/Q risk-factor rewrites — nudges it a few points.
          Click a bucket to see exactly which signals moved it and by how much; the
          chips at the bottom show every input with its freshness (○ = stale/missing,
          excluded from the rules). The ✓? button opens the report card: forward-return
          scoring of past suggestions plus a regime backtest replayed over stored
          macro history — the panel that tells you which rules to trust.
        </P>
        <Term k="tilt (+/−pp)">how far signals pushed a bucket from its regime base. Small by design — this is a tilt engine, not a market-timing model.</Term>
        <Term k="equity sleeve">the RRG quadrants translated into favor/avoid sector lists — a within-equities tilt, not extra buckets.</Term>
        <Term k="inside this sleeve">each bucket split into individual holdings: equities into RRG-weighted sector ETFs plus single-name picks scored across insider clusters, Senate PTR flow, 13F whale adds, 7d news sentiment and relative tape (hover a row for all evidence); metals into GLD/SLV by the gold/silver ratio + COT; crypto into BTC/ETH by 3m relative momentum + COT.</Term>
        <Term k="score">a pick&apos;s summed signal points — single names only appear when independent signals overlap (≥1.5), and are capped at ~10% of the equity sleeve each.</Term>
        <Term k="strategy notes">3-5 one-liners connecting signals to positioning (e.g. &quot;gamma short + risk-off: size down, expect range expansion&quot;), written by the configured LLM; the chip shows which (or <em>template</em> when the LLM is off).</Term>
        <Use>
          read it as a daily &quot;what does everything together say&quot; sanity check
          and review old snapshots in hindsight (one is stored per day). It is signal
          synthesis from lagged, mechanical inputs — NOT financial advice and NOT an
          execution target.
        </Use>
      </>
    ),
  },
  {
    id: "workflow",
    title: "A suggested daily loop",
    body: (
      <>
        <P>1. <strong>Brief</strong> — the 30-second read. 2. <strong>Event Horizon</strong> — anything red today? Then expect noise and demand more confirmation from every signal.</P>
        <P>3. <strong>Alerts</strong> — anything new overnight? Each alert names the panel to drill into.</P>
        <P>4. <strong>Macro gauge + Cookbook</strong> — has the regime or any relationship changed character?</P>
        <P>5. <strong>Smart Money + Rotation</strong> — weekly-cadence check (Fridays after COT, Mondays for rotation): where is positioning crowded, which sectors are money entering?</P>
        <P>6. <strong>Retail + News + Tape</strong> — intraday color when something is moving and you want to know why.</P>
        <p style={{ margin: "6px 0 8px", color: "var(--text-dim)" }}>
          This is a research/intelligence terminal, not an execution system: equities
          are delayed, weekly sources lag by design, and every signal here is an input
          to judgment, not a trade trigger.
        </p>
      </>
    ),
  },
];

function Section({ s, open, onToggle }: {
  s: (typeof SECTIONS)[number];
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <div style={{ borderBottom: "1px solid var(--border, #2a2a2a)" }}>
      <button
        className="expand-btn"
        onClick={onToggle}
        style={{
          display: "flex", width: "100%", justifyContent: "space-between",
          alignItems: "center", padding: "7px 2px", fontSize: 12,
          textAlign: "left", background: "none",
        }}
      >
        <span style={{ color: open ? "var(--green)" : "var(--text, #ddd)" }}>{s.title}</span>
        <span style={{ color: "var(--text-dim)" }}>{open ? "−" : "+"}</span>
      </button>
      {open && <div style={{ padding: "2px 2px 10px", fontSize: 12, lineHeight: 1.5 }}>{s.body}</div>}
    </div>
  );
}

function HelpModal({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const modal = (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>Field Manual — every panel, what it means, how to use it</span>
          <button className="expand-btn" title="Close (Esc)" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body">
          {SECTIONS.map((s) => (
            <div key={s.id} style={{ marginBottom: 16 }}>
              <div className="macro-detail-title" style={{ marginBottom: 6 }}>{s.title}</div>
              <div style={{ fontSize: 12, lineHeight: 1.55 }}>{s.body}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
  return createPortal(modal, document.body);
}

export function HelpPanel() {
  const [open, setOpen] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Field Manual</span>
        <button
          className="expand-btn"
          title="Expand: full guide in one page"
          onClick={(e) => { e.stopPropagation(); setExpanded(true); }}
        >
          ⤢
        </button>
      </div>
      <div className="panel-body">
        <div style={{ color: "var(--text-dim)", fontSize: 11, marginBottom: 6 }}>
          What every panel means and how to act on it — tap a section, or ⤢ for the
          full manual.
        </div>
        {SECTIONS.map((s) => (
          <Section
            key={s.id}
            s={s}
            open={open === s.id}
            onToggle={() => setOpen(open === s.id ? null : s.id)}
          />
        ))}
      </div>
      {expanded && <HelpModal onClose={() => setExpanded(false)} />}
    </div>
  );
}
