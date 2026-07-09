# Product Roadmap — selling the market terminal

Decisions locked 2026-07-08:

- **Distribution**: local-first desktop app (Tauri wrapping the existing Next.js UI, FastAPI+DuckDB bundled as a sidecar). Selling point: *your keys and data never leave your machine.*
- **Data rights**: BYO API keys. Customers bring their own free keys (FRED, FMP, Alpaca, …); Yahoo-dependent paths get swapped to keyed providers. We sell software, never redistribute data.
- **Target buyer (v1)**: retail power-traders / prosumers. Bloomberg-lite terminal, ~$20–50/mo price band. Polish and onboarding matter most.
- **Bots in v1**: live trading ships at launch but **hard opt-in gated** (see gate spec below). Paper mode is the default everywhere.

## Tier sketch (prices TBD)

| Tier | Contents |
|------|----------|
| Base | Dashboard, data panels, alerts, multi-window |
| Pro | Strategist LLM, divergence engine, insider/whales scanners, Lazy Prices |
| Trader | Bots, broker connect (Alpaca/IBKR), live trading (armed), portfolio attribution |

## Milestones

- **M1 — Multi-window panels** *(in progress)*: pop any panel out into its own window (browser `window.open` + BroadcastChannel sync now; becomes native Tauri windows in M2). Layout + popout state persistence.
- **M2 — Desktop packaging (Tauri)**: `apps/desktop` scaffold; dev mode wraps the Next.js server; Python API bundled as a sidecar binary (PyInstaller); native multi-window; macOS installer first (signing/notarization needs Apple Developer account), Windows later. **Blocked on: Rust toolchain install.**
- **M2.5 — Data-source remediation** *(BLOCKS charging money — see `docs/data-licensing-audit.md`, 2026-07-09: 16 GREEN / 12 YELLOW / 13 RED)*:
  - **Price engine swap (highest priority)**: yfinance is RED and load-bearing. Replace with **Tiingo** for daily history (its ToS explicitly permits the BYO-key pattern in writing) + **Alpaca market data** for live/intraday (YELLOW — get written confirmation from Alpaca first). Polygon Individual and EODHD Non-Professional are NOT safe substitutes even with BYO keys.
  - **Build profiles**: a `personal` vs `commercial` profile flag. Personal (your own machine) keeps everything; the shipped commercial build excludes RED feeds: Kalshi panel, CBOE-derived series (VVIX/SKEW/GEX — FRED's VIXCLS survives as the VIX), StockTwits, AAII, Senate PTR tracker (statutory bar, not just ToS), and the RED news RSS (Seeking Alpha, MarketWatch, Investing.com, CNBC) — GDELT+EDGAR+Finnhub remain as the news path.
  - **Follow-ups**: written confirmations needed from Alpaca (data display) and Finnhub (redistribution); Polymarket needs a legal read; FINRA Query API short interest needs a FINRA agreement or gets dropped from commercial builds.
- **M3 — Onboarding & settings UI**: kill the hand-edited `.env`. First-run wizard walks through getting each BYO key, validates each with a live test call, stores secrets in the OS keychain. Backend reads from the settings store.
- **M4 — Licensing & subscriptions**: Stripe subscriptions + license-key verification (Keygen.sh or a tiny hosted endpoint), in-app tier gates, offline grace period (app is local-first — never brick offline).
- **M5 — Brokers & the live-trading gate**: IBKR write path (Client Portal API orders) alongside Alpaca; Alpaca OAuth app so customers don't paste raw keys; implement the gate spec below.
- **M6 — Hardening & launch**: auto-update, opt-in crash reporting, docs site, landing page, disclaimers everywhere, ~10-user beta, pricing page.

## Live-trading gate spec (v1 principles)

- Disarmed by default, every install, every update. Paper is the default mode of every bot.
- Arming is a deliberate ceremony: type a confirmation phrase, set a max daily loss and per-trade risk cap, acknowledge the risk disclosure. Arming state does not survive app restart.
- Global kill switch permanently visible whenever armed; hitting daily-loss cap auto-disarms and flattens.
- Unmissable visual distinction between paper and live (persistent red banner when armed).
- Every live order logged with strategy rationale (audit trail already exists in the tradebook — extend it).
- No performance claims anywhere in marketing. The fee-audit lesson (day sleeve gross +$91 / net −$298) is exactly why.

## Cross-cutting / non-code

- **Legal**: entity (LLC), lawyer review of disclaimers + ToS + the publisher's-exemption posture, broker vendor agreements (IBKR third-party registration, Alpaca OAuth app review).
- **Data-source audit**: DONE 2026-07-09 → `docs/data-licensing-audit.md` (41 sources, per-source citations, prioritized swap list). Key lesson: BYO-key is only safe where the vendor's ToS says so — check the text, never assume.
- **Repo hygiene**: separate research code (app/ml, JEPA, scratchpad fetchers) from the shipped product path; commit or shelve the uncommitted ML work.

## User to-dos (can't be done by the agent)

1. Install Rust for Tauri: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y`
2. Apple Developer account ($99/yr) for macOS signing/notarization.
3. Stripe account + business entity.
4. Product name + domain.
5. A real lawyer for the disclaimer/ToS pass before charging money.
