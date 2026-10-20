import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";

import type { Candle, IndicatorPoint } from "../lib/types";

export interface CandleChartProps {
  candles: Candle[];
  indicators?: Record<string, IndicatorPoint[]>;
  height?: number;
  ariaLabel?: string;
  upColor?: string;
  downColor?: string;
  formatPrice: (value: number) => string;
  /** Receives the hovered bar, or null when the pointer leaves the chart. */
  onHover?: (candle: Candle | null) => void;
}

const PAPER = "#fffdf8";
const INK = "#34302c";
const INK_SOFT = "#665e57";
const MUTED = "#95897d";
const LINE = "#dfd5c8";
const SERIF = '"Source Serif 4", Georgia, serif';

export const INDICATOR_COLORS: Record<string, string> = {
  sma20: "#c76548",
  sma60: "#517264",
  rsi14: "#665e57",
};

function toTime(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;
}

/**
 * TradingView lightweight-charts wrapper.
 *
 * The chart owns zoom, crosshair, time axis and the last-price line; React
 * owns the container and the data. Moving averages share the price pane, RSI
 * gets its own pane fixed to 0..100 with 30/70 guides. Data is replaced
 * through the refs rather than `fitContent`, so panning survives updates.
 */
export function CandleChart({
  candles,
  indicators,
  height = 340,
  ariaLabel,
  upColor = "#517264",
  downColor = "#a84c46",
  formatPrice,
  onHover,
}: CandleChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lineRefs = useRef<Record<string, ISeriesApi<"Line">>>({});
  const byTime = useRef<Map<number, Candle>>(new Map());
  const hoverRef = useRef(onHover);
  hoverRef.current = onHover;
  const formatRef = useRef(formatPrice);
  formatRef.current = formatPrice;

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const chart = createChart(container, {
      width: container.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: PAPER },
        textColor: MUTED,
        fontFamily: SERIF,
        fontSize: 11,
        // The TradingView attribution logo (Apache-2.0 NOTICE requirement)
        // stays visible by default; do not turn it off.
        panes: { separatorColor: LINE, separatorHoverColor: "rgba(52, 48, 44, 0.12)" },
      },
      grid: {
        vertLines: { color: "rgba(223, 213, 200, 0.55)", style: LineStyle.Dotted },
        horzLines: { color: "rgba(223, 213, 200, 0.55)", style: LineStyle.Dotted },
      },
      rightPriceScale: { borderColor: LINE, scaleMargins: { top: 0.08, bottom: 0.26 } },
      timeScale: {
        borderColor: LINE,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 2,
        barSpacing: 7,
        minBarSpacing: 2,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: INK_SOFT, width: 1, style: LineStyle.Dashed, labelBackgroundColor: INK },
        horzLine: { color: INK_SOFT, width: 1, style: LineStyle.Dashed, labelBackgroundColor: INK },
      },
      localization: {
        priceFormatter: (price: number) => formatRef.current(price),
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true },
      handleScale: { mouseWheel: true, pinch: true },
    });
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor,
      downColor,
      borderUpColor: upColor,
      borderDownColor: downColor,
      wickUpColor: upColor,
      wickDownColor: downColor,
      priceLineStyle: LineStyle.Dashed,
      priceLineColor: INK_SOFT,
      lastValueVisible: true,
    });
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      priceLineVisible: false,
      lastValueVisible: false,
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.26 } });

    const sma20 = chart.addSeries(LineSeries, {
      color: INDICATOR_COLORS.sma20,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    const sma60 = chart.addSeries(LineSeries, {
      color: INDICATOR_COLORS.sma60,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    const rsiSeries = chart.addSeries(
      LineSeries,
      {
        color: INDICATOR_COLORS.rsi14,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: false,
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
      },
      1,
    );
    for (const level of [70, 30]) {
      rsiSeries.createPriceLine({
        price: level,
        color: LINE,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: false,
        title: "",
      });
    }
    chart.panes()[0].setHeight(Math.max(180, height - 96));
    chart.panes()[1].setHeight(96);

    const handler = (param: MouseEventParams<Time>) => {
      const emit = hoverRef.current;
      if (!emit) return;
      if (param.time === undefined) {
        emit(null);
        return;
      }
      emit(byTime.current.get(param.time as number) ?? null);
    };
    chart.subscribeCrosshairMove(handler);

    chartRef.current = chart;
    candleRef.current = candleSeries;
    volumeRef.current = volumeSeries;
    lineRefs.current = { sma20, sma60, rsi14: rsiSeries };

    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width) chart.applyOptions({ width: Math.round(width) });
    });
    observer.observe(container);

    return () => {
      observer.disconnect();
      chart.unsubscribeCrosshairMove(handler);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      lineRefs.current = {};
    };
  }, [height, upColor, downColor]);

  useEffect(() => {
    const candleSeries = candleRef.current;
    const volumeSeries = volumeRef.current;
    if (!candleSeries || !volumeSeries) return;
    const map = new Map<number, Candle>();
    const times: number[] = [];
    for (const bar of candles) {
      const time = toTime(bar.t);
      times.push(time);
      if (!map.has(time)) map.set(time, bar);
    }
    // Upsampled bars share the first member's open time; the chart needs a
    // strictly increasing time axis, so keep the first occurrence per slot.
    const series = [...map.entries()].sort(([a], [b]) => a - b);
    candleSeries.setData(
      series.map(([time, bar]) => ({
        time: time as UTCTimestamp,
        open: bar.o,
        high: bar.h,
        low: bar.l,
        close: bar.c,
      })),
    );
    volumeSeries.setData(
      series.map(([time, bar]) => ({
        time: time as UTCTimestamp,
        value: bar.v,
        color: bar.c >= bar.o ? "rgba(81, 114, 100, 0.38)" : "rgba(168, 76, 70, 0.38)",
      })),
    );
    for (const [name, line] of Object.entries(lineRefs.current)) {
      const points = indicators?.[name] ?? [];
      line.setData(
        points
          .filter((point) => point.i >= 0 && point.i < times.length)
          .map((point) => ({
            time: times[point.i] as UTCTimestamp,
            value: point.v,
          })),
      );
    }
    byTime.current = map;
  }, [candles, indicators]);

  return (
    <div
      ref={containerRef}
      className="candle-chart"
      role="img"
      aria-label={ariaLabel ?? "candlestick chart"}
      style={{ width: "100%", height }}
    />
  );
}
