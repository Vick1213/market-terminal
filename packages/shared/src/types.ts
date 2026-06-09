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

export type WsMessage = HeartbeatMessage | WelcomeMessage | NewsMessage | Record<string, unknown>;

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

export type AssetClass = "equity" | "crypto" | "metal" | "fx" | "future";

export interface WatchlistItem {
  symbol: string;
  asset_class: AssetClass;
  display_name?: string;
}
