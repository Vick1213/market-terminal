"use client";

/**
 * A compact TradingView-style price chart for a single trade, with the trade's
 * entry / take-profit / stop-loss drawn as horizontal price lines — mirroring
 * the SL/TP bracket TradingView shows.
 *
 * Data path: for day-sleeve trades pass `intraday` — we serve the same 1-minute
 * bars the day trader itself decides on (/api/series/intraday, Alpaca) and draw
 * candles. Otherwise (or if intraday is empty, e.g. market closed) we fall back
 * to the daily "PRICE:<symbol>" line, and finally to the embedded TradingView
 * widget for symbols neither path can resolve (e.g. some crypto).
 */

import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  LineSeries,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { TradeLevels } from "@market/shared";
import { fetchIntradayBars, fetchSeries } from "@/lib/api";

function chartOptions(timeVisible: boolean) {
  return {
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
    timeScale: { borderColor: "#232838", timeVisible, secondsVisible: false },
    rightPriceScale: { borderColor: "#232838" },
    autoSize: true,
  } as const;
}

/** TradingView embed symbol for the fallback widget (crypto → COINBASE:ETHUSD). */
function tvSymbol(symbol: string): string {
  if (symbol.includes("/")) {
    const [base, quote] = symbol.split("/");
    return `COINBASE:${base}${quote}`.toUpperCase();
  }
  return symbol.toUpperCase();
}

function TvEmbed({ symbol, height }: { symbol: string; height: number }) {
  const sym = encodeURIComponent(tvSymbol(symbol));
  const src =
    `https://s.tradingview.com/widgetembed/?symbol=${sym}` +
    `&interval=5&theme=dark&style=1&hideideas=1&withdateranges=0&hidesidetoolbar=1` +
    `&saveimage=0&toolbarbg=141414`;
  return (
    <iframe
      title={`${symbol} chart`}
      src={src}
      style={{ width: "100%", height, border: "none", borderRadius: 4 }}
      loading="lazy"
    />
  );
}

export function TradeChart({
  symbol,
  levels,
  intraday = false,
  minutes = 180,
  days = 120,
  height = 220,
}: {
  symbol: string;
  levels?: TradeLevels | null;
  intraday?: boolean;
  minutes?: number;
  days?: number;
  height?: number;
}) {
  // Intraday 1-min bars (day trades). Daily line is the fallback — fetched only
  // when not intraday, or when intraday came back empty (e.g. market closed).
  const intradayQ = useQuery({
    queryKey: ["trade-intraday", symbol, minutes],
    queryFn: () => fetchIntradayBars(symbol, minutes),
    enabled: intraday,
    staleTime: 60_000,
  });
  const intradayBars = intradayQ.data?.bars ?? [];
  const useDaily = !intraday || (intradayQ.isFetched && intradayBars.length === 0);
  const dailyQ = useQuery({
    queryKey: ["trade-price", symbol, days],
    queryFn: () => fetchSeries([`PRICE:${symbol}`], days),
    enabled: useDaily,
    staleTime: 5 * 60_000,
  });
  const dailyPoints = dailyQ.data?.series?.[0]?.points ?? [];

  const mode: "candles" | "line" | "none" =
    intradayBars.length > 0 ? "candles" : dailyPoints.length > 0 ? "line" : "none";
  const isLoading =
    (intraday && intradayQ.isLoading) || (useDaily && dailyQ.isLoading);

  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line" | "Candlestick"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);

  // Create the chart + series once per render-mode (only when we have data).
  useEffect(() => {
    const el = containerRef.current;
    if (!el || mode === "none") return;
    const chart = createChart(el, chartOptions(mode === "candles"));
    const series =
      mode === "candles"
        ? chart.addSeries(CandlestickSeries, {
            upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
            wickUpColor: "#26a69a", wickDownColor: "#ef5350",
          })
        : chart.addSeries(LineSeries, { color: "#2962ff", lineWidth: 2 });
    chartRef.current = chart;
    seriesRef.current = series;
    return () => {
      priceLinesRef.current = [];
      seriesRef.current = null;
      chartRef.current = null;
      chart.remove();
    };
  }, [mode]);

  // Push data + (re)draw the entry / TP / SL price lines.
  useEffect(() => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!chart || !series) return;

    if (mode === "candles") {
      // strictly-ascending times; collapse duplicate timestamps to the last.
      const clean: { time: UTCTimestamp; open: number; high: number; low: number; close: number }[] = [];
      for (const b of intradayBars) {
        const prev = clean[clean.length - 1];
        const bar = { time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c };
        if (prev && b.t === (prev.time as number)) clean[clean.length - 1] = bar;
        else if (prev && b.t < (prev.time as number)) continue;
        else clean.push(bar);
      }
      (series as ISeriesApi<"Candlestick">).setData(clean);
    } else if (mode === "line") {
      const clean: { time: UTCTimestamp; value: number }[] = [];
      for (const p of dailyPoints) {
        const prev = clean[clean.length - 1];
        if (prev && p.t === (prev.time as number)) prev.value = p.v;
        else if (prev && p.t < (prev.time as number)) continue;
        else clean.push({ time: p.t as UTCTimestamp, value: p.v });
      }
      (series as ISeriesApi<"Line">).setData(clean);
    }

    for (const pl of priceLinesRef.current) series.removePriceLine(pl);
    priceLinesRef.current = [];
    const addLine = (price: number | null | undefined, color: string, title: string) => {
      if (price == null || !Number.isFinite(price)) return;
      priceLinesRef.current.push(
        series.createPriceLine({
          price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title,
        }),
      );
    };
    addLine(levels?.entry, "#9aa0aa", "entry");
    addLine(levels?.tp, "#26a69a", "TP");
    addLine(levels?.sl, "#ef5350", "SL");

    chart.timeScale().fitContent();
  }, [mode, intradayBars, dailyPoints, levels]);

  return (
    <div style={{ marginTop: 4 }}>
      <div style={{ display: "flex", gap: 10, marginBottom: 3, fontSize: 10, flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600 }}>{symbol}</span>
        {mode === "candles" && <span style={{ color: "var(--text-dim)" }}>1m</span>}
        {levels?.entry != null && (
          <span style={{ color: "var(--text-dim)" }}>
            entry <span className="num">{levels.entry.toFixed(2)}</span>
          </span>
        )}
        {levels?.tp != null && (
          <span style={{ color: "var(--green)" }}>
            TP <span className="num">{levels.tp.toFixed(2)}</span>
          </span>
        )}
        {levels?.sl != null && (
          <span style={{ color: "var(--red)" }}>
            SL <span className="num">{levels.sl.toFixed(2)}</span>
          </span>
        )}
      </div>
      {mode !== "none" ? (
        <div ref={containerRef} style={{ height }} />
      ) : isLoading ? (
        <div className="chart-empty" style={{ height, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, color: "var(--text-dim)" }}>
          loading {symbol} price…
        </div>
      ) : (
        <TvEmbed symbol={symbol} height={height} />
      )}
    </div>
  );
}
