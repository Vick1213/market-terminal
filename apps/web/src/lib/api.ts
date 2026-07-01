import type {
  AlertsResponse,
  BriefResponse,
  CalendarResponse,
  ChartMarker,
  CongressResponse,
  CotResponse,
  GexResponse,
  InsiderResponse,
  RotationResponse,
  CorrCardDetail,
  CorrResponse,
  HealthResponse,
  MacroResponse,
  MarkersResponse,
  MultiAssetResponse,
  NewsResponse,
  RetailResponse,
  RetailSymbolResponse,
  SeriesCatalogResponse,
  SeriesResponse,
  ShortInterestResponse,
  SourceHealthResponse,
  ReportCardResponse,
  StrategistResponse,
  VolOverlayResponse,
  FactorRanksResponse,
  WatchlistLiveResponse,
  WatchlistQuote,
  WatchlistResponse,
  WhalesResponse,
  BotStatusResponse,
  BotProposeResponse,
  BotActionResponse,
  BotOptimizer,
  DayStatusResponse,
  DayRunResponse,
  PortfolioOverview,
  DivergenceResponse,
  DivergenceSnapshot,
  PolymarketResponse,
  Edgar8KResponse,
  Edgar13DResponse,
  TreasuryAuctionsResponse,
  KalshiResponse,
  EiaEnergyResponse,
  CftcTffResponse,
  ClevelandNowcastResponse,
  PolicyRiskResponse,
  FedSpeechesResponse,
  TradesResponse,
  CloseTradeResponse,
  DayReview,
  SwingReview,
  SuggestionsResponse,
  SuggestionActionResponse,
  KnobsResponse,
} from "@market/shared";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://127.0.0.1:8000";

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch(`${API_URL}/api/health`, { cache: "no-store" });
  if (!res.ok) throw new Error(`health request failed: ${res.status}`);
  return res.json();
}

export async function fetchSourceHealth(): Promise<SourceHealthResponse> {
  const res = await fetch(`${API_URL}/api/health/sources`, { cache: "no-store" });
  if (!res.ok) throw new Error(`source health request failed: ${res.status}`);
  return res.json();
}

export async function fetchMacro(): Promise<MacroResponse> {
  const res = await fetch(`${API_URL}/api/macro`, { cache: "no-store" });
  if (!res.ok) throw new Error(`macro request failed: ${res.status}`);
  return res.json();
}

export async function fetchSeriesCatalog(): Promise<SeriesCatalogResponse> {
  const res = await fetch(`${API_URL}/api/series/catalog`, { cache: "no-store" });
  if (!res.ok) throw new Error(`series catalog failed: ${res.status}`);
  return res.json();
}

export async function fetchSeries(ids: string[], days = 365): Promise<SeriesResponse> {
  const qs = new URLSearchParams({ ids: ids.join(","), days: String(days) });
  const res = await fetch(`${API_URL}/api/series?${qs}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`series request failed: ${res.status}`);
  return res.json();
}

export interface IntradayBar { t: number; o: number; h: number; l: number; c: number; v: number }
export interface IntradayResponse { symbol: string; bars: IntradayBar[] }

/** 1-minute bars for the day-trader trade chart (Alpaca intraday, last `minutes`). */
export async function fetchIntradayBars(symbol: string, minutes = 180): Promise<IntradayResponse> {
  const qs = new URLSearchParams({ symbol, minutes: String(minutes) });
  const res = await fetch(`${API_URL}/api/series/intraday?${qs}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`intraday request failed: ${res.status}`);
  return res.json();
}

export async function fetchMarkers(chartKey: string): Promise<MarkersResponse> {
  const qs = new URLSearchParams({ chart_key: chartKey });
  const res = await fetch(`${API_URL}/api/markers?${qs}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`markers request failed: ${res.status}`);
  return res.json();
}

export async function createMarker(marker: Omit<ChartMarker, "id">): Promise<ChartMarker> {
  const res = await fetch(`${API_URL}/api/markers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(marker),
  });
  if (!res.ok) throw new Error(`marker create failed: ${res.status}`);
  return res.json();
}

export async function deleteMarker(id: string): Promise<void> {
  const res = await fetch(`${API_URL}/api/markers/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`marker delete failed: ${res.status}`);
}

export async function fetchWatchlist(): Promise<WatchlistResponse> {
  const res = await fetch(`${API_URL}/api/watchlist`, { cache: "no-store" });
  if (!res.ok) throw new Error(`watchlist request failed: ${res.status}`);
  return res.json();
}

export async function fetchWatchlistLive(): Promise<WatchlistLiveResponse> {
  const res = await fetch(`${API_URL}/api/watchlist/live`, { cache: "no-store" });
  if (!res.ok) throw new Error(`watchlist live request failed: ${res.status}`);
  return res.json();
}

export async function addWatchlistSymbol(item: {
  symbol: string;
  asset_class: string;
  display_name?: string;
}): Promise<WatchlistQuote> {
  const res = await fetch(`${API_URL}/api/watchlist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(item),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `watchlist add failed: ${res.status}`);
  }
  return res.json();
}

export async function removeWatchlistSymbol(symbol: string): Promise<void> {
  const qs = new URLSearchParams({ symbol });
  const res = await fetch(`${API_URL}/api/watchlist?${qs}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`watchlist remove failed: ${res.status}`);
}

export async function fetchMultiAsset(): Promise<MultiAssetResponse> {
  const res = await fetch(`${API_URL}/api/multiasset`, { cache: "no-store" });
  if (!res.ok) throw new Error(`multiasset request failed: ${res.status}`);
  return res.json();
}

export async function fetchRetail(limit = 30): Promise<RetailResponse> {
  const res = await fetch(`${API_URL}/api/retail?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`retail request failed: ${res.status}`);
  return res.json();
}

export async function fetchRetailSymbol(symbol: string): Promise<RetailSymbolResponse> {
  const res = await fetch(`${API_URL}/api/retail/${encodeURIComponent(symbol)}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`retail drill failed: ${res.status}`);
  return res.json();
}

export async function fetchCorr(): Promise<CorrResponse> {
  const res = await fetch(`${API_URL}/api/corr`, { cache: "no-store" });
  if (!res.ok) throw new Error(`corr request failed: ${res.status}`);
  return res.json();
}

export async function fetchCorrCard(cardId: string): Promise<CorrCardDetail> {
  const res = await fetch(`${API_URL}/api/corr/${encodeURIComponent(cardId)}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`corr card failed: ${res.status}`);
  return res.json();
}

export async function fetchAlerts(limit = 50): Promise<AlertsResponse> {
  const res = await fetch(`${API_URL}/api/alerts?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`alerts request failed: ${res.status}`);
  return res.json();
}

export async function testNtfy(): Promise<{ ok: boolean; detail?: string }> {
  const res = await fetch(`${API_URL}/api/alerts/test`, { method: "POST" });
  if (!res.ok) throw new Error(`ntfy test failed: ${res.status}`);
  return res.json();
}

export async function fetchRotation(): Promise<RotationResponse> {
  const res = await fetch(`${API_URL}/api/rotation`, { cache: "no-store" });
  if (!res.ok) throw new Error(`rotation request failed: ${res.status}`);
  return res.json();
}

export async function fetchCot(): Promise<CotResponse> {
  const res = await fetch(`${API_URL}/api/cot`, { cache: "no-store" });
  if (!res.ok) throw new Error(`cot request failed: ${res.status}`);
  return res.json();
}

export async function fetchShortInterest(): Promise<ShortInterestResponse> {
  const res = await fetch(`${API_URL}/api/short-interest`, { cache: "no-store" });
  if (!res.ok) throw new Error(`short interest request failed: ${res.status}`);
  return res.json();
}

export async function fetchGex(): Promise<GexResponse> {
  const res = await fetch(`${API_URL}/api/gex`, { cache: "no-store" });
  if (!res.ok) throw new Error(`gex request failed: ${res.status}`);
  return res.json();
}

export async function fetchInsider(days = 30): Promise<InsiderResponse> {
  const res = await fetch(`${API_URL}/api/insider?days=${days}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`insider request failed: ${res.status}`);
  return res.json();
}

export async function fetchCongress(days = 90, limit = 60): Promise<CongressResponse> {
  const res = await fetch(`${API_URL}/api/congress?days=${days}&limit=${limit}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`congress request failed: ${res.status}`);
  return res.json();
}

export async function fetchWhales(): Promise<WhalesResponse> {
  const res = await fetch(`${API_URL}/api/whales`, { cache: "no-store" });
  if (!res.ok) throw new Error(`whales request failed: ${res.status}`);
  return res.json();
}

export async function fetchCalendar(days = 30): Promise<CalendarResponse> {
  const res = await fetch(`${API_URL}/api/calendar?days=${days}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`calendar request failed: ${res.status}`);
  return res.json();
}

export async function fetchBrief(): Promise<BriefResponse> {
  const res = await fetch(`${API_URL}/api/brief`, { cache: "no-store" });
  if (!res.ok) throw new Error(`brief request failed: ${res.status}`);
  return res.json();
}

export async function runBrief(): Promise<BriefResponse> {
  const res = await fetch(`${API_URL}/api/brief/run`, { method: "POST" });
  if (!res.ok) throw new Error(`brief run failed: ${res.status}`);
  return res.json();
}

export async function fetchStrategist(): Promise<StrategistResponse> {
  const res = await fetch(`${API_URL}/api/strategist`, { cache: "no-store" });
  if (!res.ok) throw new Error(`strategist request failed: ${res.status}`);
  return res.json();
}

export async function runStrategist(): Promise<StrategistResponse> {
  const res = await fetch(`${API_URL}/api/strategist/run`, { method: "POST" });
  if (!res.ok) throw new Error(`strategist run failed: ${res.status}`);
  return res.json();
}

export async function fetchStrategistReport(): Promise<ReportCardResponse> {
  const res = await fetch(`${API_URL}/api/strategist/report`, { cache: "no-store" });
  if (!res.ok) throw new Error(`strategist report failed: ${res.status}`);
  return res.json();
}

// --- Phase 12: paper trading bot ---
export async function fetchBotStatus(): Promise<BotStatusResponse> {
  const res = await fetch(`${API_URL}/api/bot/status`, { cache: "no-store" });
  if (!res.ok) throw new Error(`bot status failed: ${res.status}`);
  return res.json();
}

export async function runBotPropose(): Promise<BotProposeResponse> {
  const res = await fetch(`${API_URL}/api/bot/propose`, { method: "POST" });
  if (!res.ok) throw new Error(`bot propose failed: ${res.status}`);
  return res.json();
}

export async function executeBotProposal(id: number): Promise<BotActionResponse> {
  const res = await fetch(`${API_URL}/api/bot/execute/${id}`, { method: "POST" });
  if (!res.ok) throw new Error(`bot execute failed: ${res.status}`);
  return res.json();
}

export async function reconcileBot(): Promise<BotActionResponse> {
  const res = await fetch(`${API_URL}/api/bot/reconcile`, { method: "POST" });
  if (!res.ok) throw new Error(`bot reconcile failed: ${res.status}`);
  return res.json();
}

export async function setBotEnabled(enabled: boolean): Promise<BotActionResponse> {
  const res = await fetch(`${API_URL}/api/bot/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`bot ${enabled ? "enable" : "disable"} failed: ${res.status}`);
  return res.json();
}

export async function setBotMode(mode: string): Promise<BotActionResponse> {
  const res = await fetch(`${API_URL}/api/bot/mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  if (!res.ok) throw new Error(`bot mode failed: ${res.status}`);
  return res.json();
}

// Swing: arm/disarm enforced protective exits (stop-loss / trailing / profit-take
// / RRG rotation). Needs the env master swing_managed_exits on too. Default OFF.
export async function setManagedExits(enabled: boolean): Promise<BotActionResponse> {
  const res = await fetch(`${API_URL}/api/bot/managed-exits/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`managed-exits ${enabled ? "enable" : "disable"} failed: ${res.status}`);
  return res.json();
}

// Swing: arm/disarm posture-scaled gross + per-sector cap when building proposals.
// Needs the env master swing_posture_sizing on too. Default OFF (legacy sizing).
export async function setPostureSizing(enabled: boolean): Promise<BotActionResponse> {
  const res = await fetch(`${API_URL}/api/bot/posture-sizing/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`posture-sizing ${enabled ? "enable" : "disable"} failed: ${res.status}`);
  return res.json();
}

export async function fetchBotOptimizer(): Promise<BotOptimizer> {
  const res = await fetch(`${API_URL}/api/bot/optimizer`, { cache: "no-store" });
  if (!res.ok) throw new Error(`optimizer fetch failed: ${res.status}`);
  return res.json();
}

export async function runBotOptimizer(): Promise<BotOptimizer> {
  const res = await fetch(`${API_URL}/api/bot/optimizer/run`, { method: "POST" });
  if (!res.ok) throw new Error(`optimizer run failed: ${res.status}`);
  return res.json();
}

export async function fetchPortfolio(): Promise<PortfolioOverview> {
  const res = await fetch(`${API_URL}/api/bot/portfolio`, { cache: "no-store" });
  if (!res.ok) throw new Error(`portfolio fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchDayStatus(): Promise<DayStatusResponse> {
  const res = await fetch(`${API_URL}/api/bot/day/status`, { cache: "no-store" });
  if (!res.ok) throw new Error(`day status failed: ${res.status}`);
  return res.json();
}

export async function runDay(): Promise<DayRunResponse> {
  const res = await fetch(`${API_URL}/api/bot/day/run`, { method: "POST" });
  if (!res.ok) throw new Error(`day run failed: ${res.status}`);
  return res.json();
}

export async function setDayEnabled(enabled: boolean): Promise<{ enabled: boolean }> {
  const res = await fetch(`${API_URL}/api/bot/day/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`day ${enabled ? "enable" : "disable"} failed: ${res.status}`);
  return res.json();
}

// Arm/disarm the day sleeve's net-beta SH hedge (off by default — see BotPanel).
export async function setDayHedge(enabled: boolean): Promise<{ hedge_enabled: boolean }> {
  const res = await fetch(`${API_URL}/api/bot/day/hedge/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`day hedge ${enabled ? "enable" : "disable"} failed: ${res.status}`);
  return res.json();
}

// Conviction-gated leverage: arm/disarm the panel toggle. Env master + the
// validated-edge gate still apply on top, so this alone never levers blind.
export async function setDayLeverage(enabled: boolean): Promise<{ leverage: boolean }> {
  const res = await fetch(`${API_URL}/api/bot/day/leverage/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`day leverage ${enabled ? "enable" : "disable"} failed: ${res.status}`);
  return res.json();
}

// Soft stop: pause NEW day entries but keep managing open positions (true = paused).
export async function setDaySoftStop(paused: boolean): Promise<{ soft_stop: boolean }> {
  const res = await fetch(`${API_URL}/api/bot/day/soft-stop/${paused ? "enable" : "disable"}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`day soft-stop ${paused ? "enable" : "disable"} failed: ${res.status}`);
  return res.json();
}

// Shorts: arm/disarm SHORT entries for the day sleeve (equities only). Default OFF.
export async function setDayShorts(enabled: boolean): Promise<{ short_enabled: boolean }> {
  const res = await fetch(`${API_URL}/api/bot/day/shorts/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`day shorts ${enabled ? "enable" : "disable"} failed: ${res.status}`);
  return res.json();
}

// Pairs: arm/disarm relative-strength market-neutral mode (needs shorts armed).
export async function setDayPairs(enabled: boolean): Promise<{ pairs_enabled: boolean }> {
  const res = await fetch(`${API_URL}/api/bot/day/pairs/${enabled ? "enable" : "disable"}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`day pairs ${enabled ? "enable" : "disable"} failed: ${res.status}`);
  return res.json();
}

// --- Tradebook: paired entry/exit trades + the day-sleeve learning loop ---
export async function fetchTrades(sleeve?: string, status?: string): Promise<TradesResponse> {
  const qs = new URLSearchParams();
  if (sleeve) qs.set("sleeve", sleeve);
  if (status) qs.set("status", status);
  const q = qs.toString();
  const res = await fetch(`${API_URL}/api/bot/trades${q ? `?${q}` : ""}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`trades request failed: ${res.status}`);
  return res.json();
}

// Manually close one open bot trade (SELL a long / BUY-to-cover a short). The
// UI confirms before calling. De-risking, so the backend runs it regardless of
// the kill switch and cancels the sleeve's resting protective legs first.
export async function closeTrade(
  sleeve: string,
  symbol: string,
  qty?: number,
): Promise<CloseTradeResponse> {
  const res = await fetch(`${API_URL}/api/bot/trades/close`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(qty != null ? { sleeve, symbol, qty } : { sleeve, symbol }),
  });
  const data = (await res.json().catch(() => ({}))) as CloseTradeResponse;
  if (!res.ok) throw new Error(data?.detail || `close trade failed: ${res.status}`);
  return data;
}

// --- Learning loop v2: suggestion ledger + runtime knobs ---
export async function fetchSuggestions(
  sleeve?: string,
  status?: string,
): Promise<SuggestionsResponse> {
  const qs = new URLSearchParams();
  if (sleeve) qs.set("sleeve", sleeve);
  if (status) qs.set("status", status);
  const q = qs.toString();
  const res = await fetch(`${API_URL}/api/bot/suggestions${q ? `?${q}` : ""}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`suggestions request failed: ${res.status}`);
  return res.json();
}

async function suggestionAction(id: number, action: "accept" | "reject" | "revert") {
  const res = await fetch(`${API_URL}/api/bot/suggestions/${id}/${action}`, { method: "POST" });
  const data = (await res.json().catch(() => ({}))) as SuggestionActionResponse;
  if (!res.ok || data.ok === false) throw new Error(data?.detail || `suggestion ${action} failed`);
  return data;
}
export const acceptSuggestion = (id: number) => suggestionAction(id, "accept");
export const rejectSuggestion = (id: number) => suggestionAction(id, "reject");
export const revertSuggestion = (id: number) => suggestionAction(id, "revert");

export async function fetchKnobs(): Promise<KnobsResponse> {
  const res = await fetch(`${API_URL}/api/bot/knobs`, { cache: "no-store" });
  if (!res.ok) throw new Error(`knobs request failed: ${res.status}`);
  return res.json();
}

export async function fetchDayReview(): Promise<DayReview> {
  const res = await fetch(`${API_URL}/api/bot/day/review`, { cache: "no-store" });
  if (!res.ok) throw new Error(`day review request failed: ${res.status}`);
  return res.json();
}

export async function runDayReview(): Promise<DayReview> {
  const res = await fetch(`${API_URL}/api/bot/day/review/run`, { method: "POST" });
  if (!res.ok) throw new Error(`day review run failed: ${res.status}`);
  return res.json();
}

export async function fetchSwingReview(): Promise<SwingReview> {
  const res = await fetch(`${API_URL}/api/bot/swing/review`, { cache: "no-store" });
  if (!res.ok) throw new Error(`swing review request failed: ${res.status}`);
  return res.json();
}

export async function runSwingReview(): Promise<SwingReview> {
  const res = await fetch(`${API_URL}/api/bot/swing/review/run`, { method: "POST" });
  if (!res.ok) throw new Error(`swing review run failed: ${res.status}`);
  return res.json();
}

// --- Phase 15: narrative-vs-money divergence + Polymarket ---
export async function fetchDivergence(): Promise<DivergenceResponse> {
  const res = await fetch(`${API_URL}/api/divergence`, { cache: "no-store" });
  if (!res.ok) throw new Error(`divergence request failed: ${res.status}`);
  return res.json();
}

export async function runDivergence(): Promise<DivergenceSnapshot> {
  const res = await fetch(`${API_URL}/api/divergence/run`, { method: "POST" });
  if (!res.ok) throw new Error(`divergence run failed: ${res.status}`);
  return res.json();
}

export async function fetchPolymarket(): Promise<PolymarketResponse> {
  const res = await fetch(`${API_URL}/api/polymarket`, { cache: "no-store" });
  if (!res.ok) throw new Error(`polymarket request failed: ${res.status}`);
  return res.json();
}

export async function runPolymarket(): Promise<{ tracked: number }> {
  const res = await fetch(`${API_URL}/api/polymarket/run`, { method: "POST" });
  if (!res.ok) throw new Error(`polymarket run failed: ${res.status}`);
  return res.json();
}

// --- Phase 16 §11 rank 12: 8-K item-code corporate-event catalysts ---
export async function fetchEdgar8K(limit = 60): Promise<Edgar8KResponse> {
  const res = await fetch(`${API_URL}/api/edgar/8k?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`edgar 8-K request failed: ${res.status}`);
  return res.json();
}

export async function runEdgar8K(): Promise<{ ok: boolean; stored: number }> {
  const res = await fetch(`${API_URL}/api/edgar/8k/run`, { method: "POST" });
  if (!res.ok) throw new Error(`edgar 8-K run failed: ${res.status}`);
  return res.json();
}

// --- Phase 16 §11 rank 13: SC 13D/13G activist/passive stakes ---
export async function fetchEdgar13D(limit = 60): Promise<Edgar13DResponse> {
  const res = await fetch(`${API_URL}/api/edgar/13d?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`edgar 13D request failed: ${res.status}`);
  return res.json();
}

export async function runEdgar13D(): Promise<{ ok: boolean; stored: number }> {
  const res = await fetch(`${API_URL}/api/edgar/13d/run`, { method: "POST" });
  if (!res.ok) throw new Error(`edgar 13D run failed: ${res.status}`);
  return res.json();
}

// --- Phase 16 §11 rank 7: Treasury auction demand ---
export async function fetchTreasuryAuctions(limit = 40): Promise<TreasuryAuctionsResponse> {
  const res = await fetch(`${API_URL}/api/treasury/auctions?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`treasury auctions request failed: ${res.status}`);
  return res.json();
}

export async function runTreasuryAuctions(): Promise<{ ok: boolean }> {
  const res = await fetch(`${API_URL}/api/treasury/auctions/run`, { method: "POST" });
  if (!res.ok) throw new Error(`treasury auctions run failed: ${res.status}`);
  return res.json();
}

// --- Phase 16 §11 rank 8: Kalshi FOMC/CPI distributions ---
export async function fetchKalshi(): Promise<KalshiResponse> {
  const res = await fetch(`${API_URL}/api/kalshi`, { cache: "no-store" });
  if (!res.ok) throw new Error(`kalshi request failed: ${res.status}`);
  return res.json();
}

export async function runKalshi(): Promise<{ ok: boolean; reason?: string }> {
  const res = await fetch(`${API_URL}/api/kalshi/run`, { method: "POST" });
  if (!res.ok) throw new Error(`kalshi run failed: ${res.status}`);
  return res.json();
}

// --- Phase 16 §11 rank 10: EIA weekly energy fundamentals ---
export async function fetchEiaEnergy(limit = 52): Promise<EiaEnergyResponse> {
  const res = await fetch(`${API_URL}/api/eia/energy?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`eia energy request failed: ${res.status}`);
  return res.json();
}

export async function runEiaEnergy(): Promise<{ ok: boolean }> {
  const res = await fetch(`${API_URL}/api/eia/energy/run`, { method: "POST" });
  if (!res.ok) throw new Error(`eia energy run failed: ${res.status}`);
  return res.json();
}

// --- Phase 16 §11 rank 6: CFTC TFF positioning ---
export async function fetchCftcTff(): Promise<CftcTffResponse> {
  const res = await fetch(`${API_URL}/api/cftc/tff`, { cache: "no-store" });
  if (!res.ok) throw new Error(`cftc tff request failed: ${res.status}`);
  return res.json();
}

export async function runCftcTff(): Promise<{ ok: boolean }> {
  const res = await fetch(`${API_URL}/api/cftc/tff/run`, { method: "POST" });
  if (!res.ok) throw new Error(`cftc tff run failed: ${res.status}`);
  return res.json();
}

// --- Phase 16 §11 rank 9: Cleveland Fed inflation nowcast ---
export async function fetchClevelandNowcast(limit = 120): Promise<ClevelandNowcastResponse> {
  const res = await fetch(`${API_URL}/api/cleveland/nowcast?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`cleveland nowcast request failed: ${res.status}`);
  return res.json();
}

export async function runClevelandNowcast(): Promise<{ ok: boolean }> {
  const res = await fetch(`${API_URL}/api/cleveland/nowcast/run`, { method: "POST" });
  if (!res.ok) throw new Error(`cleveland nowcast run failed: ${res.status}`);
  return res.json();
}

// --- Phase 16 §11 rank 11: policy-risk surface (GPR + TPU) ---
export async function fetchPolicyRisk(limit = 120): Promise<PolicyRiskResponse> {
  const res = await fetch(`${API_URL}/api/policy-risk?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`policy-risk request failed: ${res.status}`);
  return res.json();
}

// --- §12 follow-through: volatility-targeted exposure overlay ---
export async function fetchVolOverlay(
  symbol = "SPY",
  targetVol = 0.15,
): Promise<VolOverlayResponse> {
  const res = await fetch(
    `${API_URL}/api/vol-overlay?symbol=${symbol}&target_vol=${targetVol}`,
    { cache: "no-store" },
  );
  if (!res.ok) throw new Error(`vol-overlay request failed: ${res.status}`);
  return res.json();
}

export async function fetchFactors(): Promise<FactorRanksResponse> {
  const res = await fetch(`${API_URL}/api/factors`, { cache: "no-store" });
  if (!res.ok) throw new Error(`factors request failed: ${res.status}`);
  return res.json();
}

export async function runPolicyRisk(): Promise<{ ok: boolean }> {
  const res = await fetch(`${API_URL}/api/policy-risk/run`, { method: "POST" });
  if (!res.ok) throw new Error(`policy-risk run failed: ${res.status}`);
  return res.json();
}

// --- Phase 16 §11 rank 11: Fed speeches hawk/dove ---
export async function fetchFedSpeeches(limit = 30): Promise<FedSpeechesResponse> {
  const res = await fetch(`${API_URL}/api/fed/speeches?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`fed speeches request failed: ${res.status}`);
  return res.json();
}

export async function runFedSpeeches(): Promise<{ ok: boolean }> {
  const res = await fetch(`${API_URL}/api/fed/speeches/run`, { method: "POST" });
  if (!res.ok) throw new Error(`fed speeches run failed: ${res.status}`);
  return res.json();
}

export async function fetchNews(symbol?: string, limit = 100): Promise<NewsResponse> {
  const qs = new URLSearchParams();
  if (symbol) qs.set("symbol", symbol);
  qs.set("limit", String(limit));
  const res = await fetch(`${API_URL}/api/news?${qs}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`news request failed: ${res.status}`);
  return res.json();
}

export async function addNewsTicker(symbol: string): Promise<{ symbol: string; new_items: number }> {
  const res = await fetch(`${API_URL}/api/news/tickers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol }),
  });
  if (!res.ok) {
    const detail = (await res.json().catch(() => null))?.detail;
    throw new Error(detail ?? `add news ticker failed: ${res.status}`);
  }
  return res.json();
}

export async function removeNewsTicker(symbol: string): Promise<void> {
  const res = await fetch(`${API_URL}/api/news/tickers?symbol=${encodeURIComponent(symbol)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`remove news ticker failed: ${res.status}`);
}
