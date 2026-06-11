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
  StrategistResponse,
  WatchlistQuote,
  WatchlistResponse,
  WhalesResponse,
} from "@market/shared";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://127.0.0.1:8000";

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch(`${API_URL}/api/health`, { cache: "no-store" });
  if (!res.ok) throw new Error(`health request failed: ${res.status}`);
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
