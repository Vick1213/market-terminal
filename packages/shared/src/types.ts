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

export interface MacroResponse {
  score: number | null; // -100..+100, + = risk-on; null while warming up
  regime: Regime | string;
  computed_at: string | null;
  buckets: MacroBucket[];
  dials: Record<string, MacroRegimeDial>;
  headline: MacroHeadlineDial[];
  history: MacroHistoryPoint[];
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
