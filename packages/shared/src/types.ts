// Hand-written shared contracts. Once the backend is running, regenerate the
// authoritative versions from the live OpenAPI schema:
//   pnpm gen:types   ->   packages/shared/src/api-types.ts
// and prefer those. These stay as a convenience for the Phase-0 panel.

export interface HealthResponse {
  status: string;
  app: string;
  version: string;
  time: string;
  data_dir: string;
  scheduler_running: boolean;
  ws_clients: number;
  watchlist_count: number;
  duckdb_tables: Record<string, number>;
}

export interface ShortInterestItem {
  symbol: string;
  settlement_date: string;
  shares_short: number | null;
  change_pct: number | null;
  days_to_cover: number | null;
  dtc_percentile: number | null;
  prints: number;
}

export interface ShortInterestResponse {
  symbols: ShortInterestItem[];
}

export type SourceStatus = "ok" | "degraded" | "dead";

export interface SourceHealthItem {
  host: string;
  label: string;
  covers: string;
  status: SourceStatus;
  last_success: string | null;
  last_failure: string | null;
  last_error: string | null;
  consecutive_failures: number;
  success_count: number;
  failure_count: number;
}

export interface SourceHealthResponse {
  status: SourceStatus;
  sources: SourceHealthItem[];
}

export interface HeartbeatMessage {
  type: "heartbeat";
  n: number;
  ts: string;
  ws_clients: number;
}

export interface WelcomeMessage {
  type: "welcome";
  topic: string;
}

export type WsMessage =
  | HeartbeatMessage
  | WelcomeMessage
  | NewsMessage
  | MacroMessage
  | TradeMessage
  | DepthMessage
  | QuoteMessage
  | RetailWsMessage
  | CorrWsMessage
  | AlertWsMessage
  | BriefWsMessage
  | Record<string, unknown>;

// --- Phase 1: news + sentiment ---

export type SentimentLabel = "positive" | "negative" | "neutral";

export interface NewsItem {
  id: string;
  source: string; // yahoo | edgar | finnhub
  symbol: string | null;
  title: string;
  summary?: string | null;
  url: string | null;
  published: string; // ISO
  score: number | null; // p_pos - p_neg, -1..+1
  confidence: number | null; // max softmax
  label: SentimentLabel | string | null;
  outlets?: number; // multi-outlet convergence count (>=2 = confirmed story)
  outlet_names?: string | null; // comma-separated sources that carried it
}

export interface NewsMessage {
  type: "news";
  item: NewsItem;
}

export interface NewsResponse {
  items: NewsItem[];
  symbols: string[];
  custom?: string[]; // user-added news-only tickers (removable chips)
}

export interface SentimentResult {
  text_hash: string;
  score: number;
  confidence: number;
  label: string;
  model: string;
  cached: boolean;
  flagged: boolean;
}

// --- Phase 2: macro / liquidity + regime ---

export type Regime = "risk-on" | "neutral" | "risk-off" | "stress" | "unknown" | "warming-up";

export interface MacroComponent {
  series_id: string;
  sign: number; // +1 = high is risk-on, -1 = inverted
  value: number;
  z: number; // sign-aligned, clamped ±3
  ts: string; // ISO date of the latest observation
}

export interface MacroBucket {
  key: string;
  label: string;
  weight: number; // planned (PLAN §3c)
  weight_used: number; // after renormalizing over available buckets
  z: number | null; // null = no data yet (e.g. breadth before Phase 3)
  contribution: number; // points of the composite
  components: MacroComponent[];
}

export interface MacroRegimeDial {
  label: string;
  value: number;
  vote: number; // +1 risk-on / -1 risk-off
}

export interface MacroHeadlineDial {
  series_id: string;
  label: string;
  value: number;
  ts: string;
}

export interface MacroHistoryPoint {
  ts: string;
  score: number;
}

/** Daily Fed net liquidity = WALCL − TGA − RRP, all $bn (Phase 8). */
export interface LiquidityBlock {
  as_of: string;
  net_bn: number;
  d5_bn: number | null; // change vs 5 daily prints ago (~1 week)
  d20_bn: number | null; // change vs 20 daily prints ago (~1 month)
  walcl_bn: number | null;
  tga_bn: number | null;
  rrp_bn: number | null;
}

export interface MacroResponse {
  score: number | null; // -100..+100, + = risk-on; null while warming up
  regime: Regime | string;
  computed_at: string | null;
  buckets: MacroBucket[];
  dials: Record<string, MacroRegimeDial>;
  headline: MacroHeadlineDial[];
  history: MacroHistoryPoint[];
  liquidity?: LiquidityBlock | null;
}

export interface MacroMessage {
  type: "macro";
  score: number;
  regime: string;
  computed_at: string;
}

// --- chart model: composable series + persistent markers ---

export interface SeriesPoint {
  t: number; // unix seconds (UTC)
  v: number;
}

export interface SeriesData {
  id: string; // macro id | "PRICE:SYM" | "SENT:SYM" | "SENT:ALL"
  label: string;
  group: "macro" | "price" | "sentiment" | string;
  points: SeriesPoint[];
}

export interface SeriesResponse {
  series: SeriesData[];
}

export interface SeriesCatalogItem {
  id: string;
  label: string;
}

export interface SeriesCatalogGroup {
  key: string;
  label: string;
  items: SeriesCatalogItem[];
}

export interface SeriesCatalogResponse {
  groups: SeriesCatalogGroup[];
}

export interface ChartMarker {
  id: string;
  chart_key: string;
  t: number; // unix seconds
  price: number | null;
  series_id: string | null;
  text: string;
}

export interface MarkersResponse {
  markers: ChartMarker[];
}

export type AssetClass = "equity" | "crypto" | "metal" | "fx" | "future";

export interface WatchlistItem {
  symbol: string;
  asset_class: AssetClass;
  display_name?: string;
}

// --- Phase 3: watchlist panel ---

export interface WatchlistQuote {
  symbol: string;
  asset_class: AssetClass | string;
  display_name: string | null;
  close: number | null;
  prev_close: number | null;
  change_pct: number | null; // last close vs prior close
  open: number | null;
  high: number | null;
  low: number | null;
  volume: number | null;
  ts: string | null; // ISO timestamp of the latest daily bar
  spark: number[]; // last ~30 daily closes, oldest first
  sent_score: number | null;
  sent_label: string | null;
  sent_title: string | null;
  sent_url: string | null;
  sent_published: string | null;
}

export interface WatchlistResponse {
  quotes: WatchlistQuote[];
}

export interface WatchlistLiveQuote {
  symbol: string;
  price: number;
  prev_close: number | null;
  change_pct: number | null; // live price vs previous close
  day_high: number | null;
  day_low: number | null;
  volume: number | null;
  ts: string; // ISO timestamp of the fetch, not the trade
}

export interface WatchlistLiveResponse {
  quotes: WatchlistLiveQuote[];
}

// --- Phase 4: multi-asset liquidity & major trades ---

export interface CryptoQuote {
  exchange: string;
  symbol: string;
  price: number | null;
  ts: string | null;
  trades_seen: number;
  connected: boolean;
}

export interface DepthSnapshot {
  exchange: string;
  symbol: string;
  ts: string;
  imbalance: number; // (bid-ask)/(bid+ask) over top levels, -1..+1
  bid_depth: number;
  ask_depth: number;
  mid: number;
  spread_bp: number;
}

export interface TradePrint {
  exchange: string;
  symbol: string;
  ts: string;
  price: number;
  size: number;
  side: string | null;
  notional: number;
  z?: number | null; // only on WS-pushed prints
}

export interface TradeMessage extends Omit<TradePrint, "side"> {
  type: "trade";
  side: string;
}

export interface DepthMessage extends DepthSnapshot {
  type: "depth";
}

export interface QuoteMessage {
  type: "quote";
  quotes: CryptoQuote[];
}

export interface CryptoSection {
  live: boolean;
  quotes: CryptoQuote[];
  books: DepthSnapshot[];
  prints: TradePrint[];
  btc_dominance: number | null;
  volume_24h_usd: number | null;
}

export interface MetalRow {
  symbol: string; // XAU | XAG
  spot: number | null;
  spot_ts: string | null;
  futures_close: number | null;
  futures_ts: string | null;
  basis: number | null; // futures - spot
  basis_pct: number | null;
  etf_symbol: string | null; // GLD | SLV flow proxy
  etf_volume_z: number | null;
}

export interface EquityFlow {
  symbol: string;
  volume_z: number | null;
  short_ratio: number | null;
  short_trend_z: number | null;
  accumulation: number | null; // + = accumulation
  ts: string | null;
}

export interface MultiAssetResponse {
  crypto: CryptoSection;
  metals: MetalRow[];
  equities: EquityFlow[];
  freshness: Record<string, string>;
}

// --- Phase 5: retail market score ---

export interface RetailGauge {
  score: number | null; // -100..+100 mention-weighted bull/bear
  sentiment: number | null;
  chatter_z: number | null;
  total_mentions: number | null;
  scored_symbols: number;
  computed_at: string | null;
}

export interface RetailLeader {
  symbol: string;
  asset_class: string;
  mentions: number;
  mentions_24h_ago: number | null;
  mention_z: number;
  rank: number | null;
  rank_velocity: number | null;
  upvotes: number | null;
  sentiment: number | null;
  sentiment_sources: string[];
  sources: number; // cross-source confirmation count
  divergence: boolean;
  spike_score: number;
}

export interface RetailResponse {
  gauge: RetailGauge;
  leaderboard: RetailLeader[];
  freshness: Record<string, string | null>;
}

export interface RetailHistoryPoint {
  t: number; // unix seconds
  mentions: number;
  rank: number | null;
}

export interface RetailSourceStat {
  source: string;
  ts: string;
  mentions: number | null;
  sentiment: number | null;
}

export interface RetailMessage {
  source: string;
  ts: string;
  text: string;
  url: string | null;
  score: number | null;
  label: string | null;
  tag: string | null;
}

export interface RetailHeadline {
  title: string;
  url: string | null;
  published: string;
  score: number | null;
  label: string | null;
}

export interface RetailSymbolResponse {
  symbol: string;
  history: RetailHistoryPoint[];
  sources: RetailSourceStat[];
  messages: RetailMessage[];
  headlines: RetailHeadline[];
}

export interface RetailWsMessage {
  type: "retail";
  computed_at: string | null;
  score: number | null;
  total_mentions: number | null;
}

// --- Phase 6: correlation cookbook ---

export type CorrStatus = "holds" | "watch" | "broken" | "no-data";

export interface CorrLeadLag {
  unit: "d" | "w";
  peak_lag: number; // >0 = the named leader's past correlates with the other leg's present
  peak_corr: number | null;
  leader: string;
  leads_now: boolean;
  decayed: boolean; // leading series stopped leading = early decay warning
  profile: { lag: number; corr: number | null }[];
}

export interface CorrCard {
  id: string;
  num: number;
  title: string;
  mode: "corr" | "ratio" | "curve" | "term" | string;
  status: CorrStatus | string;
  legs: string[];
  asof: string | null;
  windows: string[]; // ["30d","90d"] or ["13w","26w"] for the weekly card
  corr30: number | null;
  corr90: number | null;
  baseline_mean: number | null;
  baseline_std: number | null;
  baseline_n: number;
  z: number | null; // z of current corr vs long-run rolling-90 baseline
  sign_flip: boolean;
  value: number | null; // ratio / term / curve level
  value_label: string;
  lead_lag: CorrLeadLag | null;
  secondary: { label: string; corr30: number | null; corr90: number | null } | null;
  notes: string[];
  normal: string;
  rationale: string;
  breaks_when: string;
}

export interface CorrStressLight {
  key: string;
  label: string;
  on: boolean;
  value: number | null;
}

export interface CorrResponse {
  computed_at: string | null;
  regime: string;
  regime_score: number | null;
  cards: CorrCard[];
  stress: { on: boolean; lights: CorrStressLight[] };
  heatmap: { labels: string[]; matrix: (number | null)[][] };
}

export interface CorrHistoryPoint {
  t: number; // unix seconds
  corr30: number | null;
  corr90: number | null;
}

export interface CorrCardDetail {
  card: CorrCard;
  history?: CorrHistoryPoint[];
  legs_rebased?: { t: number; a: number | null; b: number | null }[];
  levels?: { t: number; v?: number | null; a?: number | null; b?: number | null }[];
  leg_labels: string[];
}

export interface CorrWsMessage {
  type: "corr";
  computed_at: string;
  regime: string;
  broken: number;
  watch: number;
  stress: boolean;
}

// --- Phase 7: edge extras --------------------------------------------------

export type AlertSeverity = "info" | "warn" | "critical";

export interface AlertItem {
  id: string;
  ts: string;
  rule: string;
  severity: AlertSeverity;
  title: string;
  body: string | null;
  symbol: string | null;
  value: number | null;
  regime: string | null;
  pushed: boolean;
}

export interface AlertsResponse {
  ntfy_enabled: boolean;
  alerts: AlertItem[];
}

export interface AlertWsMessage {
  type: "alert";
  id: string | null;
  rule: string;
  severity: AlertSeverity;
  title: string;
  body: string;
  symbol: string | null;
  value: number | null;
  ts: string;
}

export type RrgQuadrant = "leading" | "weakening" | "lagging" | "improving";

export interface RrgSector {
  symbol: string;
  name: string;
  rs_ratio: number;
  rs_momentum: number;
  quadrant: RrgQuadrant;
  trail: { ts: string; r: number; m: number }[];
}

export interface SeasonalityMonth {
  month: number;
  mean_return: number;
  win_rate: number;
  years: number;
}

export interface SeasonalityRow {
  symbol: string;
  months: SeasonalityMonth[];
}

export interface RotationResponse {
  benchmark: string;
  sectors: RrgSector[];
  seasonality: SeasonalityRow[];
}

export interface CotMarket {
  market_code: string;
  market: string;
  report_date: string;
  net_noncommercial: number;
  cot_index: number | null;
  net_13w_ago: number | null;
  open_interest: number | null;
  weeks: number;
}

export interface CotResponse {
  markets: CotMarket[];
}

export interface GexSnapshot {
  symbol: string;
  ts: string;
  spot: number | null;
  total_gex_bn: number | null;
  flip: number | null;
  call_wall: number | null;
  put_wall: number | null;
  profile: { strike: number; gex: number }[];
  contracts: number | null;
}

export interface GexResponse {
  snapshots: GexSnapshot[];
}

export interface InsiderTrade {
  accession: string;
  symbol: string;
  issuer: string | null;
  insider: string;
  title: string | null;
  is_ceo_cfo: boolean;
  filed_at: string;
  trade_date: string | null;
  shares: number | null;
  price: number | null;
  value: number | null;
  url: string | null;
}

export interface InsiderCluster {
  symbol: string;
  buyers: number;
  total_value: number | null;
  last_filed: string;
  has_ceo_cfo: boolean;
}

export interface InsiderResponse {
  trades: InsiderTrade[];
  clusters: InsiderCluster[];
}

// --- Phase 8: smart money 2.0 ---

export interface CongressTrade {
  ptr_id: string;
  row: number;
  senator: string;
  filed_at: string;
  tx_date: string | null;
  ticker: string | null; // null = non-equity asset
  asset: string | null;
  asset_type: string | null;
  side: string | null; // buy | sell | exchange
  amount_min: number | null; // disclosed band, USD
  amount_max: number | null; // null on open-ended bands
  url: string | null;
  on_watchlist: boolean;
}

export interface CongressResponse {
  trades: CongressTrade[];
}

export interface WhaleHolding {
  issuer: string | null;
  cls: string;
  put_call: string; // '' | Put | Call
  value: number | null; // USD
  pct: number | null; // % of portfolio
  rank: number;
  shares_chg_pct: number | null; // QoQ share-count change; null = no prior
  status: "held" | "new";
}

export interface WhaleFund {
  cik: number;
  fund: string;
  period: string; // report quarter end
  filed_at: string;
  total_value: number | null; // USD
  positions: number | null;
  prev_period: string | null;
  holdings: WhaleHolding[];
  new_count: number;
  exits: string[]; // issuers that left the stored top-N
}

export interface WhalesResponse {
  funds: WhaleFund[];
}

export interface CalendarEvent {
  id: string;
  date: string;
  days_until: number;
  kind: "fomc" | "cpi" | "nfp" | "ppi" | "gdp" | "pce" | "opex" | "cot" | "earnings";
  title: string;
  symbol: string | null;
  source: string;
}

export interface CalendarResponse {
  events: CalendarEvent[];
}

export interface BriefResponse {
  date: string | null;
  regime: string | null;
  text: string | null;
  model: string | null;
  digest?: Record<string, unknown>;
}

export interface BriefWsMessage {
  type: "brief";
  date: string;
  regime: string;
  model: string;
}

// --- Phase 9: strategist ---

export interface StrategistReason {
  signal: string; // key of the signal that drove this tilt
  detail: string; // human reasoning string
  delta: number; // percentage points added/removed (0 = the regime base)
}

export interface StrategistHolding {
  symbol: string;
  name?: string | null;
  kind: "sector" | "stock" | "asset" | string;
  sleeve_pct: number; // share of this bucket
  weight_pct: number; // share of the whole portfolio
  score?: number | null; // conviction score (single-name picks only)
  evidence: string[]; // the signal lines that produced this holding
}

export interface StrategistBucket {
  key: "equities" | "metals" | "crypto" | "cash" | string;
  label: string;
  base_pct: number; // regime base before tilts
  weight_pct: number; // final suggested weight (sums to ~100)
  reasons: StrategistReason[];
  holdings?: StrategistHolding[]; // individual assets inside the sleeve
}

export interface StrategistSignal {
  key: string;
  label: string;
  value: string | number | null;
  detail: string;
  asof: string | null; // freshness of this input
  stale: boolean; // too old/missing — excluded from the rules
}

export interface StrategistEquityTilt {
  benchmark: string;
  favor: string[]; // leading/improving RRG sectors
  avoid: string[]; // lagging RRG sectors
}

export interface StrategistResponse {
  status?: "warming-up"; // only set when no snapshot exists yet
  as_of?: string;
  regime?: string;
  score?: number | null;
  buckets?: StrategistBucket[];
  equity_tilt?: StrategistEquityTilt | null;
  signals?: StrategistSignal[];
  notes?: string[]; // 3-5 strategy notes (LLM or template)
  model?: string; // LLM label or "template"
  disclaimer?: string;
}

export interface StrategistWsMessage {
  type: "strategist";
  date: string;
  regime: string;
  model: string;
}

// --- Strategist report card ---

export interface ReportCardRegime {
  regime: string;
  days: number;
  n_1w: number;
  alloc_1w: number | null; // avg fwd return of the regime's base allocation
  spy_1w: number | null; // avg fwd SPY return over the same days
  n_1m: number;
  alloc_1m: number | null;
  spy_1m: number | null;
}

export interface ReportCardSignal {
  signal: string;
  fired: number; // snapshots where this signal moved the allocation
  spy_1w_after_fired: number | null;
  spy_1w_after_quiet: number | null;
}

export interface ReportCardSnapshot {
  date: string;
  regime: string;
  fired: string[];
  ret_1w: number | null;
  spy_1w: number | null;
  ret_1m: number | null;
  spy_1m: number | null;
}

export interface ReportCardResponse {
  as_of: string;
  summary: {
    snapshots: number;
    scored: number;
    avg_excess_1w: number | null;
    hit_rate_1w: number | null;
  };
  snapshots: ReportCardSnapshot[];
  regimes: ReportCardRegime[];
  signals: ReportCardSignal[];
  note: string;
}

// --- Phase 12: paper trading bot ---
export interface BotConfig {
  enabled: boolean; // kill switch
  mode: string; // "proposal" | "auto"
  updated_at: string | null;
}

export interface BotBroker {
  enabled: boolean; // keys configured
  is_paper: boolean;
  base_url: string;
}

export interface BotGuardrails {
  max_position_pct: number;
  max_position_notional: number;
  min_order_notional: number;
  daily_loss_limit_pct: number;
  rebalance_band_pp: number;
  allow_live: boolean;
}

export interface BotAccount {
  equity: number | null;
  last_equity: number | null;
  cash: number | null;
  buying_power: number | null;
  day_pnl_pct: number | null;
  daytrade_count?: number | null;
  pattern_day_trader?: boolean | null;
  status?: string | null;
  trading_blocked?: boolean | null;
}

export interface BotPosition {
  symbol: string;
  qty: number | null;
  market_value: number | null;
  avg_entry_price: number | null;
  unrealized_pl: number | null;
  unrealized_plpc?: number | null;
}

export interface BotRationale {
  kind?: string | null;
  score?: number | null;
  evidence: string[];
  bear_case?: string;
  invalidation?: string;
}

// One proposed trade. status: proposed | blocked | skipped | submitted |
// filled | rejected | canceled | expired | submitting | unknown.
export interface BotProposal {
  id: number;
  run_id?: string;
  created_at?: string;
  symbol: string;
  strategist_sym?: string | null;
  bucket?: string | null;
  side: string; // buy | sell
  order_type?: string;
  qty: number | null;
  notional: number | null;
  target_pct: number | null;
  current_pct: number | null;
  target_value: number | null;
  current_value: number | null;
  delta_value: number | null;
  conviction: number | null;
  max_loss_est: number | null;
  rationale?: BotRationale | null;
  status: string;
  blocks?: string[] | null;
  strategist_asof?: string | null;
  updated_at?: string;
  sleeve?: string;
  stale?: boolean; // fresh state no longer calls for this (already satisfied)
}

export interface BotOrder {
  id: number;
  proposal_id: number | null;
  client_order_id: string | null;
  broker_order_id: string | null;
  symbol: string;
  side: string;
  order_type: string | null;
  qty: number | null;
  notional: number | null;
  status: string | null;
  filled_qty: number | null;
  filled_avg_price: number | null;
  submitted_at: string | null;
  reconciled_at?: string | null;
  error: string | null;
}

export interface BotUntrackedPosition {
  symbol: string;
  value: number | null;
  qty: number | null;
}

export interface BotOptimizerSignal {
  key: string;
  value: string;
  detail?: string;
}

export interface BotOptimizer {
  ts?: string | null;
  swing_pct: number;
  day_pct: number;
  regime?: string | null;
  reason?: string;
  signals?: BotOptimizerSignal[];
}

export interface BotStatusResponse {
  config: BotConfig;
  broker: BotBroker;
  guardrails: BotGuardrails;
  sleeve?: string;
  optimizer?: BotOptimizer;
  proposals: BotProposal[];
  recent_orders: BotOrder[];
  account?: BotAccount;
  positions?: BotPosition[];
  account_error?: string;
  disclaimer: string;
}

export interface DaySignal {
  kind: string;
  direction: string;
  strength: number;
  last?: number | null;
  vwap?: number | null;
  z?: number;
  detail?: string;
}

export interface DayAction {
  symbol: string;
  strategist_sym?: string;
  asset_class?: string;
  signal?: DaySignal;
  news?: { title: string; score: number; outlets: number } | null;
  act?: boolean;
  side?: string | null;
  notional?: number | null;
  qty?: number | null;
  reason?: string;
  submitted?: boolean;
  max_loss_est?: number;
}

export interface DayProposal {
  id: number;
  symbol: string;
  side: string;
  qty: number | null;
  notional: number | null;
  conviction: number | null;
  max_loss_est: number | null;
  rationale?: Record<string, unknown> | null;
  status: string;
  created_at?: string;
}

export interface DayStatusResponse {
  config: { enabled: boolean };
  universe: string[];
  optimizer?: BotOptimizer;
  guardrails?: {
    max_position_pct: number;
    min_order_notional: number;
    daily_loss_limit_pct: number;
  };
  market_open?: boolean;
  holdings?: Record<string, number>;
  recent_orders: BotOrder[];
  recent_proposals: DayProposal[];
  account_error?: string;
  disclaimer: string;
}

export interface DayRunResponse {
  ok: boolean;
  detail?: string;
  config?: { enabled: boolean };
  market_open?: boolean;
  day_pct?: number;
  day_budget?: number;
  deployed?: number;
  actions?: DayAction[];
  note?: string;
  disclaimer?: string;
}

export interface BotProposeResponse {
  ok: boolean;
  detail?: string;
  run_id?: string;
  config: BotConfig;
  account?: BotAccount;
  strategist_as_of?: string | null;
  n_actionable?: number;
  equity?: number;
  buying_power?: number;
  halted?: string | null;
  proposals?: BotProposal[];
  untracked_positions?: BotUntrackedPosition[];
  disclaimer?: string;
  auto_executed?: number;
  executions?: { proposal_id: number; ok?: boolean; detail?: string }[];
  note?: string;
}

export interface BotActionResponse {
  ok?: boolean;
  detail?: string;
  proposal_id?: number;
  order?: Record<string, unknown>;
  blocks?: string[];
  enabled?: boolean;
  mode?: string;
  updated_at?: string | null;
  canceled_orders?: number;
  reconciled?: number;
}

export interface BotWsMessage {
  type: "bot";
  event: string;
}

// --- Phase 14: aggregated portfolio overview (big panel) ---
export type PortfolioSleeve = "day" | "swing" | "manual";

export interface PortfolioExitPlan {
  stop_pct: number | null;
  stop_price: number | null;
  target_value: number | null;
  target_pct: number | null;
  invalidation: string | null;
}

export interface PortfolioPosition {
  symbol: string;
  qty: number;
  market_value: number | null;
  avg_entry_price: number | null;
  unrealized_pl: number | null;
  unrealized_plpc: number | null;
  sleeve: PortfolioSleeve;
  sleeve_label: string;
  day_qty: number;
  swing_qty: number;
  manual_qty: number;
  winning: boolean;
  exit: PortfolioExitPlan | null;
}

export interface PortfolioSleeveRollup {
  label: string;
  value: number;
  unrealized_pl: number;
  n: number;
}

export interface PortfolioOverview {
  ok: boolean;
  detail?: string;
  status?: number;
  broker: BotBroker;
  optimizer?: BotOptimizer;
  total_value?: number;
  equity?: number;
  last_equity?: number | null;
  cash?: number | null;
  buying_power?: number | null;
  day_pnl_pct?: number | null;
  unrealized_pl?: number;
  unrealized_pl_pct?: number | null;
  n_positions?: number;
  n_winners?: number;
  n_losers?: number;
  sleeves?: Record<PortfolioSleeve, PortfolioSleeveRollup>;
  positions?: PortfolioPosition[];
  winners?: PortfolioPosition[];
  account?: BotAccount;
  disclaimer: string;
}

// --- Phase 15: narrative-vs-money divergence + Polymarket front-running ---
export interface PolymarketMarket {
  id: string;
  question: string;
  slug: string | null;
  url: string | null;
  escalation_sign: number; // +1 Yes=escalation, -1 Yes=de-escalation, 0 undirected
  end_date: string | null;
  volume: number | null;
  yes_prob: number | null;
  prev_prob: number | null;
  move: number | null; // Δ implied prob vs ~24h ago
  top_share: number | null; // largest holder share of the tracked side
  n_holders: number | null;
  ts: string;
}

export interface PolymarketResponse {
  markets: PolymarketMarket[];
}

export interface DivergenceLeg {
  symbol: string;
  ret_2d: number;
  z: number;
  signed: number;
}

export interface DivergenceSignals {
  money: number;
  pm: number;
  calm: number;
  brace: number;
}

export interface DivergenceSnapshot {
  ts: string;
  score: number | null;
  narrative: number | null;
  riskoff_z: number | null;
  pm_pressure: number | null;
  regime: string | null;
  headline: string | null;
  detail: {
    riskoff_legs?: DivergenceLeg[];
    narrative?: { tone: number | null; n_headlines: number; samples: string[] };
    polymarket?: { pressure: number | null; markets: PolymarketMarket[] };
    signals?: DivergenceSignals;
  };
}

export interface DivergenceHistoryPoint {
  ts: string;
  score: number | null;
  narrative: number | null;
  riskoff_z: number | null;
  pm_pressure: number | null;
}

export interface WeekendGap {
  from: string;
  to: string;
  prev_close: number;
  next_open: number;
  gap_pct: number;
}

export interface DivergenceResponse {
  status?: string;
  latest?: DivergenceSnapshot;
  history: DivergenceHistoryPoint[];
  weekend_gaps: WeekendGap[];
}

// --- Phase 16 §11 rank 12: 8-K item-code corporate-event catalysts ---
export interface Edgar8KEvent {
  accession: string;
  ticker: string | null;
  company: string | null;
  item_code: string; // the matched catalyst item (1.05/2.06/5.02/8.01)
  catalyst: string; // human label for item_code
  items: string[]; // all item codes reported on the filing
  filed_at: string;
  url: string | null;
}

export interface Edgar8KResponse {
  events: Edgar8KEvent[];
}

// --- Phase 16 §11 rank 13: SC 13D/13G activist/passive stake filings ---
export interface Edgar13DFiling {
  accession: string;
  subject_ticker: string | null;
  subject_name: string | null;
  filer_name: string | null;
  filing_type: string; // precise EDGAR form, e.g. "SC 13D", "SC 13D/A", "SC 13G"
  is_activist: boolean; // true for 13D (intent to influence), false for 13G (passive)
  pct_owned: number | null; // % of class beneficially owned; null for pre-2025 HTML-only filings
  shares: number | null; // aggregate shares owned; null when XML absent
  purpose: string | null; // 13D transaction purpose; null for 13G (passive) / HTML-only
  filed_at: string;
  url: string | null;
}

export interface Edgar13DResponse {
  filings: Edgar13DFiling[];
}

// --- Phase 16 §11 rank 7: Treasury auction demand ---
export interface TreasuryAuction {
  auction_date: string;
  security_type: string; // "Bill" | "Note" | "Bond" | "TIPS" | "FRN"
  security_term: string; // e.g. "10-Year", "4-Week"
  bid_to_cover: number | null;
  high_yield: number | null; // null for bills (quoted as discount rate)
  offering_amt: number | null;
  total_accepted: number | null;
  dealer_pct: number | null; // primary-dealer takedown % (high = weak real-money demand)
  indirect_pct: number | null; // indirect bidders (foreign / real money)
  direct_pct: number | null;
}

export interface TreasuryAuctionsResponse {
  auctions: TreasuryAuction[];
}

// --- Phase 16 §11 rank 8: Kalshi FOMC/CPI implied distributions ---
export interface KalshiBucket {
  floor_strike: number | null;
  label: string;
  cum_prob: number | null; // cumulative "Above X" probability
  bucket_prob: number; // differenced per-bucket implied probability
}

export interface KalshiEvent {
  series_ticker: string; // e.g. "KXFED", "KXCPI"
  event_ticker: string;
  close_time: string | null;
  strikes: KalshiBucket[];
  modal: KalshiBucket | null; // highest-mass bucket
}

export interface KalshiResponse {
  events: KalshiEvent[];
  fomc_expected_rate: { ts: string; expected_rate: number } | null;
}

// --- Phase 16 §11 rank 10: EIA weekly energy fundamentals ---
export interface EiaStockRow {
  date: string;
  crude_commercial?: number;
  crude_commercial_chg?: number; // weekly build (+) / draw (−)
  crude_spr?: number;
  gasoline?: number;
  distillate?: number;
}

export interface EiaSpotRow {
  date: string;
  wti?: number;
  brent?: number;
  rbob?: number;
  heating_oil?: number;
  crack_321?: number; // 3:2:1 crack spread ($/bbl)
}

export interface EiaNatgasRow {
  date: string;
  storage?: number; // working gas (Bcf)
  change?: number; // weekly net change (Bcf)
  vs_5yr_pct?: number; // surplus/deficit vs 5-yr average
  vs_yrago_pct?: number;
}

export interface EiaEnergyResponse {
  stocks: EiaStockRow[];
  spot: EiaSpotRow[];
  natgas: EiaNatgasRow[];
}

// --- Phase 16 §11 rank 6: CFTC Traders-in-Financial-Futures positioning ---
export interface TffMarket {
  ticker: string; // ES / NQ / UST10Y / VIX / DXY / BTC ...
  report_date: string;
  am_net: number; // Asset-Manager (real money) net position
  lm_net: number; // Leveraged-Money (hedge funds) net position
  dealer_net: number | null;
  am_lm_div: number; // AM_NET − LM_NET (the signal)
  div_index: number | null; // 3-year percentile of the divergence (0–100, 50 = neutral)
  div_13w_ago: number | null;
  weeks: number;
}

export interface CftcTffResponse {
  markets: TffMarket[];
}

// --- Phase 16 §11 rank 9: Cleveland Fed daily inflation nowcast ---
export interface ClevelandRow {
  date: string;
  cpi?: number;
  cpi_actual?: number;
  core_cpi?: number;
  core_cpi_actual?: number;
  pce?: number;
  pce_actual?: number;
  core_pce?: number;
  core_pce_actual?: number;
}

export interface ClevelandNowcastResponse {
  mom: ClevelandRow[];
  yoy: ClevelandRow[];
}

// --- Phase 16 §11 rank 11: policy-risk surface (GPR + TPU) ---
export interface GprRow {
  date: string;
  gpr?: number; // headline geopolitical-risk index
  threats?: number; // GPR Threats sub-index (leads Acts 1–3 months)
  acts?: number;
}

export interface TpuRow {
  date: string;
  tpu?: number; // daily trade-policy-uncertainty index
}

export interface PolicyRiskResponse {
  gpr: GprRow[];
  tpu: TpuRow[];
}

// --- Phase 16 §11 rank 11: Fed speeches scored hawk/dove ---
export interface FedSpeech {
  url: string;
  date: string;
  speaker: string;
  title: string;
  score: number; // FinBERT sentiment (−1..+1)
  hawk_dove: number; // proxy: >0 hawkish, <0 dovish
  confidence: number;
  chunks: number;
}

export interface FedSpeechesResponse {
  speeches: FedSpeech[];
  index: { date: string; value: number } | null; // rolling FED_HAWK_DOVE
}
