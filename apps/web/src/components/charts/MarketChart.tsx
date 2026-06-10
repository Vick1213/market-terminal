"use client";

/**
 * The shared TradingView-style chart (lightweight-charts) behind every panel.
 *
 * - native zoom (wheel) / pan (drag) / crosshair
 * - composable: "+ add" overlays any catalog series (price, sentiment, macro)
 *   on its own auto-scaled axis, so a sentiment view can carry a ticker chart
 * - the series mix is remembered per chartKey (localStorage)
 * - persistent markers: toggle 📍, click the chart, write a note — stored in
 *   the backend and shown on every chart with the same chartKey, forever
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ColorType,
  createChart,
  createSeriesMarkers,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { ChartMarker, SeriesData } from "@market/shared";
import {
  createMarker,
  deleteMarker,
  fetchMarkers,
  fetchSeries,
  fetchSeriesCatalog,
} from "@/lib/api";

const PALETTE = ["#2962ff", "#26a69a", "#f0b90b", "#ef5350", "#ab47bc", "#26c6da", "#ff7043"];

const CHART_OPTIONS = {
  layout: {
    background: { type: ColorType.Solid, color: "transparent" },
    textColor: "#787b86",
    fontSize: 10,
  },
  grid: {
    vertLines: { color: "#232838" },
    horzLines: { color: "#232838" },
  },
  crosshair: { mode: 0 }, // free crosshair, not magnet — feels like TradingView
  timeScale: { borderColor: "#232838", timeVisible: true },
  rightPriceScale: { borderColor: "#232838" },
  leftPriceScale: { borderColor: "#232838", visible: true },
  autoSize: true,
} as const;

function loadSavedIds(chartKey: string, fallback: string[]): string[] {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(`chart-series:${chartKey}`);
    const ids = raw ? (JSON.parse(raw) as string[]) : null;
    return ids && ids.length > 0 ? ids : fallback;
  } catch {
    return fallback;
  }
}

export function MarketChart({
  chartKey,
  initialSeriesIds,
  days = 365,
  height = 300,
}: {
  chartKey: string;
  initialSeriesIds: string[];
  days?: number;
  height?: number;
}) {
  const queryClient = useQueryClient();
  const [ids, setIds] = useState<string[]>(() => loadSavedIds(chartKey, initialSeriesIds));
  const [markMode, setMarkMode] = useState(false);
  const markModeRef = useRef(markMode);
  markModeRef.current = markMode;

  useEffect(() => {
    try {
      window.localStorage.setItem(`chart-series:${chartKey}`, JSON.stringify(ids));
    } catch {
      /* private mode etc. — chart still works, mix just won't persist */
    }
  }, [chartKey, ids]);

  const { data: catalog } = useQuery({
    queryKey: ["series-catalog"],
    queryFn: fetchSeriesCatalog,
    staleTime: 5 * 60_000,
  });
  const { data, isLoading } = useQuery({
    queryKey: ["series", [...ids].sort().join(","), days],
    queryFn: () => fetchSeries(ids, days),
    refetchInterval: 5 * 60_000,
    enabled: ids.length > 0,
  });
  const { data: markerData } = useQuery({
    queryKey: ["markers", chartKey],
    queryFn: () => fetchMarkers(chartKey),
  });

  const addMarker = useMutation({
    mutationFn: createMarker,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["markers", chartKey] }),
  });
  const removeMarker = useMutation({
    mutationFn: deleteMarker,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["markers", chartKey] }),
  });

  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const markersPluginRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const fittedRef = useRef<string>(""); // last series-set we fitContent() for

  // Create the chart once.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, CHART_OPTIONS);
    chartRef.current = chart;

    chart.subscribeClick((param) => {
      if (!markModeRef.current || param.time === undefined || !param.point) return;
      const primary = seriesRef.current.values().next().value as
        | ISeriesApi<"Line">
        | undefined;
      const price = primary?.coordinateToPrice(param.point.y) ?? null;
      const text = window.prompt("Marker note (optional):") ?? "";
      addMarker.mutate({
        chart_key: chartKey,
        t: param.time as number,
        price: typeof price === "number" ? price : null,
        series_id: null,
        text,
      });
      setMarkMode(false);
    });

    return () => {
      markersPluginRef.current = null;
      seriesRef.current.clear();
      chartRef.current = null;
      chart.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartKey]);

  const seriesList: SeriesData[] = useMemo(
    () => data?.series.filter((s) => s.points.length > 0) ?? [],
    [data]
  );

  // (Re)build series whenever data arrives. Scales: first series right axis,
  // second left axis, the rest hidden auto-scaled overlays.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    markersPluginRef.current = null;
    for (const s of seriesRef.current.values()) chart.removeSeries(s);
    seriesRef.current.clear();

    seriesList.forEach((s, idx) => {
      const scaleId = idx === 0 ? "right" : idx === 1 ? "left" : `ovl-${s.id}`;
      const line = chart.addSeries(LineSeries, {
        color: PALETTE[idx % PALETTE.length],
        lineWidth: 2,
        priceScaleId: scaleId,
        title: s.label,
        priceFormat:
          s.group === "sentiment"
            ? { type: "custom" as const, formatter: (v: number) => v.toFixed(2) }
            : undefined,
      });
      line.setData(
        s.points.map((p) => ({ time: p.t as UTCTimestamp, value: p.v }))
      );
      seriesRef.current.set(s.id, line);
    });

    const key = seriesList.map((s) => s.id).join(",");
    if (key && key !== fittedRef.current) {
      chart.timeScale().fitContent();
      fittedRef.current = key;
    }
  }, [seriesList]);

  // Attach persistent markers to the primary series.
  useEffect(() => {
    const primary = seriesRef.current.values().next().value as
      | ISeriesApi<"Line">
      | undefined;
    if (!primary) return;
    const ms: SeriesMarker<Time>[] = (markerData?.markers ?? []).map((m) => ({
      time: m.t as UTCTimestamp,
      position: "aboveBar",
      color: "#f0b90b",
      shape: "arrowDown",
      text: m.text || "•",
    }));
    if (markersPluginRef.current) {
      markersPluginRef.current.setMarkers(ms);
    } else {
      markersPluginRef.current = createSeriesMarkers(primary, ms);
    }
  }, [markerData, seriesList]);

  const addSeries = useCallback((id: string) => {
    if (!id) return;
    setIds((prev) => (prev.includes(id) || prev.length >= 6 ? prev : [...prev, id]));
  }, []);

  return (
    <div className="mchart">
      <div className="mchart-toolbar">
        {seriesList.map((s, idx) => (
          <span
            key={s.id}
            className="mchart-chip"
            style={{ borderColor: PALETTE[idx % PALETTE.length] }}
          >
            <i className="legend-dot" style={{ background: PALETTE[idx % PALETTE.length] }} />
            {s.label}
            {ids.length > 1 && (
              <button
                className="mchart-chip-x"
                title="Remove series"
                onClick={() => setIds((prev) => prev.filter((i) => i !== s.id))}
              >
                ✕
              </button>
            )}
          </span>
        ))}
        <select
          className="mchart-add"
          value=""
          onChange={(e) => addSeries(e.target.value)}
          title="Overlay another series on this chart"
        >
          <option value="">+ add series</option>
          {catalog?.groups.map((g) => (
            <optgroup key={g.key} label={g.label}>
              {g.items
                .filter((i) => !ids.includes(i.id))
                .map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.label}
                  </option>
                ))}
            </optgroup>
          ))}
        </select>
        <span style={{ flex: 1 }} />
        <button
          className={`expand-btn ${markMode ? "active-mark" : ""}`}
          title={markMode ? "Click the chart to drop a marker" : "Add a persistent marker"}
          onClick={() => setMarkMode((m) => !m)}
        >
          📍{markMode ? " click chart…" : ""}
        </button>
      </div>

      <div
        ref={containerRef}
        style={{ height, cursor: markMode ? "crosshair" : undefined }}
      />
      {isLoading && <div className="chart-empty">loading series…</div>}
      {!isLoading && seriesList.length === 0 && (
        <div className="chart-empty">no data for the selected series yet</div>
      )}

      {(markerData?.markers.length ?? 0) > 0 && (
        <div className="mchart-markers">
          {markerData!.markers.map((m: ChartMarker) => (
            <span key={m.id} className="mchart-marker" title={new Date(m.t * 1000).toLocaleString()}>
              📍 {new Date(m.t * 1000).toLocaleDateString()} {m.text && `— ${m.text}`}
              <button
                className="mchart-chip-x"
                title="Delete marker"
                onClick={() => removeMarker.mutate(m.id)}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
