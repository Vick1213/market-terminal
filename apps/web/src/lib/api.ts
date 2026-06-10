import type {
  ChartMarker,
  HealthResponse,
  MacroResponse,
  MarkersResponse,
  NewsResponse,
  SeriesCatalogResponse,
  SeriesResponse,
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

export async function fetchNews(symbol?: string, limit = 100): Promise<NewsResponse> {
  const qs = new URLSearchParams();
  if (symbol) qs.set("symbol", symbol);
  qs.set("limit", String(limit));
  const res = await fetch(`${API_URL}/api/news?${qs}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`news request failed: ${res.status}`);
  return res.json();
}
