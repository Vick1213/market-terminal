"use client";

/**
 * On-demand Kronos model forecast: pick a symbol + horizon, hit "Run
 * forecast", get a sampled future candle path drawn onto the trailing real
 * history. This is deliberately NOT a polled panel — the backend's first
 * call per process lazy-loads model weights and runs CPU inference, which
 * can take 30-120s, so the fetch only ever fires on a user click and the
 * mutation carries no timeout/retry.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  CandlestickSeries,
  ColorType,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { ForecastBar, ForecastQuantilePoint } from "@market/shared";
import { fetchForecast, fetchForecastDistribution, fetchWatchlist } from "@/lib/api";

const HORIZONS = [10, 20, 30, 45, 60, 90];

// "auto" sends no `model` param — the server picks its configured default
// (shipped default is Kronos-small; a machine can override via .env, see
// apps/api/app/forecast/service.py VARIANTS).
const MODEL_OPTIONS: { value: "auto" | "mini" | "small" | "base"; label: string }[] = [
  { value: "auto", label: "auto (server default)" },
  { value: "mini", label: "mini (fastest)" },
  { value: "small", label: "small" },
  { value: "base", label: "base (most accurate)" },
];

// Forecast candles reuse the panel's up/down colors but translucent, so the
// sampled path reads as "the same chart, a different confidence" rather than
// a second series.
const FORECAST_COLOR = {
  up: "rgba(38, 166, 154, 0.35)",
  down: "rgba(239, 83, 80, 0.35)",
  border: "rgba(120, 123, 134, 0.55)",
  wick: "rgba(120, 123, 134, 0.55)",
};

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
  crosshair: { mode: 0 },
  timeScale: { borderColor: "#232838", timeVisible: true },
  rightPriceScale: { borderColor: "#232838" },
  autoSize: true,
} as const;

function toCandle(b: ForecastBar, forecast: boolean): CandlestickData {
  const base = {
    time: b.t as UTCTimestamp,
    open: b.open,
    high: b.high,
    low: b.low,
    close: b.close,
  };
  if (!forecast) return base;
  return {
    ...base,
    color: b.close >= b.open ? FORECAST_COLOR.up : FORECAST_COLOR.down,
    borderColor: FORECAST_COLOR.border,
    wickColor: FORECAST_COLOR.wick,
  };
}

/** Candlestick chart: solid real history, translucent sampled forecast tail. */
function ForecastChart({
  history,
  forecast,
  height = 320,
}: {
  history: ForecastBar[];
  forecast: ForecastBar[];
  height?: number;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, CHART_OPTIONS);
    chartRef.current = chart;
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: true,
      wickVisible: true,
    });
    seriesRef.current = series;
    // Create the markers primitive once per chart lifetime; re-running
    // createSeriesMarkers on every data update stacked a new primitive
    // each time. Markers are pushed via .setMarkers() below instead.
    markersRef.current = createSeriesMarkers(series, []);
    return () => {
      chartRef.current = null;
      seriesRef.current = null;
      markersRef.current = null;
      chart.remove();
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!chart || !series) return;

    const data = [
      ...history.map((b) => toCandle(b, false)),
      ...forecast.map((b) => toCandle(b, true)),
    ];
    series.setData(data);

    markersRef.current?.setMarkers(
      forecast.length > 0
        ? [
            {
              time: forecast[0].t as UTCTimestamp,
              position: "aboveBar",
              color: "#f0b90b",
              shape: "arrowDown",
              text: "forecast →",
            },
          ]
        : []
    );
    chart.timeScale().fitContent();
  }, [history, forecast]);

  return <div ref={containerRef} style={{ height }} />;
}

// Same translucent-forecast styling family as FORECAST_COLOR: the median
// path gets the marker's accent color solid, the p10/p90 cone is dashed and
// dimmer, p25/p75 dotted and dimmer still — reads as "one series, more or
// less certain" rather than five unrelated lines.
const QUANTILE_COLOR = {
  p50: "#f0b90b",
  outer: "rgba(240, 185, 11, 0.55)", // p10 / p90
  inner: "rgba(240, 185, 11, 0.35)", // p25 / p75
};

function toLinePoints(points: ForecastQuantilePoint[], key: "p10" | "p25" | "p50" | "p75" | "p90"): LineData[] {
  return points.map((p) => ({ time: p.t as UTCTimestamp, value: p[key] }));
}

/** Candlestick history + P10/P25/P50/P75/P90 quantile cone over the horizon. */
function DistributionChart({
  history,
  quantiles,
  height = 320,
}: {
  history: ForecastBar[];
  quantiles: ForecastQuantilePoint[];
  height?: number;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const p10Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const p25Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const p50Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const p75Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const p90Ref = useRef<ISeriesApi<"Line"> | null>(null);
  // Same markers plugin as ForecastChart, attached to this chart's own
  // candlestick series — not a second plugin, just a second instance of it.
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, CHART_OPTIONS);
    chartRef.current = chart;

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: true,
      wickVisible: true,
    });
    candleRef.current = candle;
    markersRef.current = createSeriesMarkers(candle, []);

    p90Ref.current = chart.addSeries(LineSeries, {
      color: QUANTILE_COLOR.outer,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      title: "P90",
    });
    p75Ref.current = chart.addSeries(LineSeries, {
      color: QUANTILE_COLOR.inner,
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      title: "P75",
    });
    p50Ref.current = chart.addSeries(LineSeries, {
      color: QUANTILE_COLOR.p50,
      lineWidth: 2,
      lineStyle: LineStyle.Solid,
      title: "P50",
    });
    p25Ref.current = chart.addSeries(LineSeries, {
      color: QUANTILE_COLOR.inner,
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      title: "P25",
    });
    p10Ref.current = chart.addSeries(LineSeries, {
      color: QUANTILE_COLOR.outer,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      title: "P10",
    });

    return () => {
      chartRef.current = null;
      candleRef.current = null;
      p10Ref.current = null;
      p25Ref.current = null;
      p50Ref.current = null;
      p75Ref.current = null;
      p90Ref.current = null;
      markersRef.current = null;
      chart.remove();
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    const candle = candleRef.current;
    if (!chart || !candle) return;

    candle.setData(history.map((b) => toCandle(b, false)));
    p90Ref.current?.setData(toLinePoints(quantiles, "p90"));
    p75Ref.current?.setData(toLinePoints(quantiles, "p75"));
    p50Ref.current?.setData(toLinePoints(quantiles, "p50"));
    p25Ref.current?.setData(toLinePoints(quantiles, "p25"));
    p10Ref.current?.setData(toLinePoints(quantiles, "p10"));

    markersRef.current?.setMarkers(
      quantiles.length > 0
        ? [
            {
              time: quantiles[0].t as UTCTimestamp,
              position: "aboveBar",
              color: "#f0b90b",
              shape: "arrowDown",
              text: "forecast →",
            },
          ]
        : []
    );
    chart.timeScale().fitContent();
  }, [history, quantiles]);

  return <div ref={containerRef} style={{ height }} />;
}

function fmtPct(v: number, digits = 1): string {
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

export function ForecastPanel() {
  const { data: watchlist } = useQuery({
    queryKey: ["watchlist"],
    queryFn: fetchWatchlist,
    staleTime: 5 * 60_000,
  });

  const [symbol, setSymbol] = useState("SPY");
  const [symbolTouched, setSymbolTouched] = useState(false);
  const [horizon, setHorizon] = useState(30);
  const [model, setModel] = useState<"auto" | "mini" | "small" | "base">("auto");
  // Distribution first: a single sampled path is noise, the ensemble is the
  // model's actual information. "path" stays available for anyone who wants
  // the cheaper single-sample view.
  const [mode, setMode] = useState<"path" | "distribution">("distribution");

  // Default the symbol field to the top of the watchlist once it loads,
  // unless the user already typed something of their own.
  useEffect(() => {
    if (symbolTouched) return;
    const first = watchlist?.quotes?.[0]?.symbol;
    if (first) setSymbol(first);
  }, [watchlist, symbolTouched]);

  type RunVars = { symbol: string; horizon: number; model: "auto" | "mini" | "small" | "base" };

  const run = useMutation({
    mutationFn: (vars: RunVars) =>
      fetchForecast({
        symbol: vars.symbol,
        horizon: vars.horizon,
        model: vars.model === "auto" ? undefined : vars.model,
      }),
  });

  const runDist = useMutation({
    mutationFn: (vars: RunVars) =>
      fetchForecastDistribution({
        symbol: vars.symbol,
        horizon: vars.horizon,
        model: vars.model === "auto" ? undefined : vars.model,
      }),
  });

  const pending = mode === "path" ? run.isPending : runDist.isPending;
  const isError = mode === "path" ? run.isError : runDist.isError;
  const errorMessage = mode === "path" ? (run.error as Error | null)?.message : (runDist.error as Error | null)?.message;

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const sym = symbol.trim().toUpperCase();
    if (!sym || pending) return;
    if (mode === "path") run.mutate({ symbol: sym, horizon, model });
    else runDist.mutate({ symbol: sym, horizon, model });
  };

  const retry = () => {
    const sym = symbol.trim().toUpperCase();
    if (mode === "path") run.mutate({ symbol: sym, horizon, model });
    else runDist.mutate({ symbol: sym, horizon, model });
  };

  const d = run.data;
  const boundary = useMemo(() => {
    if (!d || d.forecast.length === 0) return null;
    return new Date(d.forecast[0].t * 1000).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }, [d]);

  const dd = runDist.data;
  const distBoundary = useMemo(() => {
    if (!dd || dd.quantiles.length === 0) return null;
    return new Date(dd.quantiles[0].t * 1000).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }, [dd]);

  // Terminal-step stats derived for the compact readout row: p_up straight
  // from the backend, median return converted from log- to simple-return,
  // the P10..P90 price cone expressed as % from last close, and each level's
  // touch probability labeled by its own % distance from last close (so it
  // reads correctly even if a caller ever passes custom levels).
  const distSummary = useMemo(() => {
    if (!dd || dd.quantiles.length === 0 || dd.history.length === 0) return null;
    const lastClose = dd.history[dd.history.length - 1].close;
    const lastQ = dd.quantiles[dd.quantiles.length - 1];
    const pctFromLastClose = (v: number) => (v / lastClose - 1) * 100;
    return {
      pUp: dd.stats.p_up * 100,
      medianReturnPct: (Math.exp(dd.stats.median_return) - 1) * 100,
      p10Pct: pctFromLastClose(lastQ.p10),
      p90Pct: pctFromLastClose(lastQ.p90),
      levels: dd.levels.map((lv) => ({
        pct: pctFromLastClose(lv.level),
        pTouch: lv.p_touch * 100,
      })),
    };
  }, [dd]);

  const headModel = mode === "path" ? d?.model : dd?.model;
  const headDevice = mode === "path" ? d?.device : dd?.device;

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Forecast</span>
        {headModel && (
          <span style={{ fontSize: 10, color: "var(--text-dim)" }} title={`device: ${headDevice}`}>
            {headModel}
          </span>
        )}
      </div>
      <div className="panel-body">
        <form className="wl-add" onSubmit={onSubmit}>
          <input
            className="wl-add-input"
            style={{ width: 110 }}
            value={symbol}
            onChange={(e) => {
              setSymbol(e.target.value.toUpperCase());
              setSymbolTouched(true);
            }}
            placeholder="symbol — e.g. SPY"
            spellCheck={false}
          />
          <select
            className="wl-add-select"
            value={horizon}
            onChange={(e) => setHorizon(Number(e.target.value))}
            title="bars to predict"
          >
            {HORIZONS.map((h) => (
              <option key={h} value={h}>
                {h} bars
              </option>
            ))}
          </select>
          <select
            className="wl-add-select"
            value={model}
            onChange={(e) => setModel(e.target.value as "auto" | "mini" | "small" | "base")}
            title="Kronos checkpoint size"
          >
            {MODEL_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <select
            className="wl-add-select"
            value={mode}
            onChange={(e) => setMode(e.target.value as "path" | "distribution")}
            title="single sampled path vs. an N-path distribution"
          >
            <option value="distribution">distribution (N paths)</option>
            <option value="path">single path</option>
          </select>
          <button className="expand-btn" type="submit" disabled={pending || !symbol.trim()}>
            {pending
              ? "running… (can take a while)"
              : mode === "distribution"
                ? "Run ensemble"
                : "Run forecast"}
          </button>
        </form>

        {pending && (
          <div style={{ color: "var(--text-dim)", fontSize: 12, margin: "8px 0" }}>
            {mode === "distribution"
              ? "sampling an N-path ensemble from Kronos — first run per server also loads the model weights, this can take 30-120s."
              : "sampling a path from Kronos — first run per server also loads the model weights, this can take 30-120s."}
          </div>
        )}

        {isError && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "8px 0" }}>
            <span style={{ color: "var(--red)", fontSize: 12 }}>{errorMessage}</span>
            <button className="expand-btn" onClick={retry}>
              retry
            </button>
          </div>
        )}

        {!pending && !isError && !(mode === "path" ? d : dd) && (
          <div style={{ color: "var(--text-dim)", fontSize: 12, margin: "8px 0" }}>
            pick a symbol + horizon and hit run — nothing fetches until you do.
          </div>
        )}

        {mode === "path" && d && (
          <>
            <ForecastChart history={d.history} forecast={d.forecast} />
            <div style={{ fontSize: 11, color: "var(--text-dim)", marginTop: 6 }}>
              {d.symbol} · {d.asset_class} · context {d.context_bars} bars · horizon {d.horizon}{" "}
              bars
              {boundary && <> · forecast starts {boundary}</>}
            </div>
            <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 4 }}>
              {d.disclaimer}
            </div>
          </>
        )}

        {mode === "distribution" && dd && (
          <>
            <DistributionChart history={dd.history} quantiles={dd.quantiles} />
            {distSummary && (
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 12,
                  fontSize: 11,
                  color: "var(--text-dim)",
                  margin: "8px 0",
                }}
              >
                <span>P(up) {distSummary.pUp.toFixed(0)}%</span>
                <span>median return {fmtPct(distSummary.medianReturnPct)}</span>
                <span>
                  terminal P10…P90 [{fmtPct(distSummary.p10Pct)} … {fmtPct(distSummary.p90Pct)}]
                </span>
                {distSummary.levels.map((lv, i) => (
                  <span key={i}>
                    P(touch {fmtPct(lv.pct, 0)}) {lv.pTouch.toFixed(0)}%
                  </span>
                ))}
              </div>
            )}
            <div style={{ fontSize: 11, color: "var(--text-dim)", marginTop: 6 }}>
              {dd.symbol} · {dd.asset_class} · context {dd.context_bars} bars · horizon{" "}
              {dd.horizon} bars · {dd.paths} paths
              {distBoundary && <> · forecast starts {distBoundary}</>}
            </div>
            <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 4 }}>
              {dd.disclaimer}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
