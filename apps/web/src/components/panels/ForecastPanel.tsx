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
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { ForecastBar } from "@market/shared";
import { fetchForecast, fetchWatchlist } from "@/lib/api";

const HORIZONS = [10, 20, 30, 45, 60, 90];

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

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, CHART_OPTIONS);
    chartRef.current = chart;
    seriesRef.current = chart.addSeries(CandlestickSeries, {
      upColor: "#26a69a",
      downColor: "#ef5350",
      borderVisible: true,
      wickVisible: true,
    });
    return () => {
      chartRef.current = null;
      seriesRef.current = null;
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

    if (forecast.length > 0) {
      createSeriesMarkers(series, [
        {
          time: forecast[0].t as UTCTimestamp,
          position: "aboveBar",
          color: "#f0b90b",
          shape: "arrowDown",
          text: "forecast →",
        },
      ]);
    }
    chart.timeScale().fitContent();
  }, [history, forecast]);

  return <div ref={containerRef} style={{ height }} />;
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

  // Default the symbol field to the top of the watchlist once it loads,
  // unless the user already typed something of their own.
  useEffect(() => {
    if (symbolTouched) return;
    const first = watchlist?.quotes?.[0]?.symbol;
    if (first) setSymbol(first);
  }, [watchlist, symbolTouched]);

  const run = useMutation({
    mutationFn: (vars: { symbol: string; horizon: number }) =>
      fetchForecast({ symbol: vars.symbol, horizon: vars.horizon }),
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const sym = symbol.trim().toUpperCase();
    if (!sym || run.isPending) return;
    run.mutate({ symbol: sym, horizon });
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

  return (
    <div className="panel">
      <div className="panel-head">
        <span>Forecast</span>
        {d?.model && (
          <span style={{ fontSize: 10, color: "var(--text-dim)" }} title={`device: ${d.device}`}>
            {d.model}
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
          <button className="expand-btn" type="submit" disabled={run.isPending || !symbol.trim()}>
            {run.isPending ? "running… (can take a while)" : "Run forecast"}
          </button>
        </form>

        {run.isPending && (
          <div style={{ color: "var(--text-dim)", fontSize: 12, margin: "8px 0" }}>
            sampling a path from Kronos — first run per server also loads the model weights,
            this can take 30-120s.
          </div>
        )}

        {run.isError && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "8px 0" }}>
            <span style={{ color: "var(--red)", fontSize: 12 }}>
              {(run.error as Error).message}
            </span>
            <button
              className="expand-btn"
              onClick={() => run.mutate({ symbol: symbol.trim().toUpperCase(), horizon })}
            >
              retry
            </button>
          </div>
        )}

        {!run.isPending && !run.isError && !d && (
          <div style={{ color: "var(--text-dim)", fontSize: 12, margin: "8px 0" }}>
            pick a symbol + horizon and hit run — nothing fetches until you do.
          </div>
        )}

        {d && (
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
      </div>
    </div>
  );
}
