import type { HealthResponse, NewsResponse } from "@market/shared";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://127.0.0.1:8000";

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch(`${API_URL}/api/health`, { cache: "no-store" });
  if (!res.ok) throw new Error(`health request failed: ${res.status}`);
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
