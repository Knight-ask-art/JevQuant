import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";

import type { Candle } from "../lib/types";

export interface CandleChartProps {
  candles: Candle[];
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

function toTime(iso: string): UTCTimestamp {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;
}

/**
 * TradingView lightweight-charts wrapper.
 *
 * The chart owns zoom, crosshair, time axis and the last-price line; React
 * owns the container and the data. Data is replaced through the ref rather
 * than `fitContent`, so panning is not reset on every update.
 */
export function CandleChart({
  candles,
  height = 300,
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
    };
  }, [height, upColor, downColor]);

  useEffect(() => {
    chartRef.current?.applyOptions({ height });
  }, [height]);

  useEffect(() => {
    const candleSeries = candleRef.current;
    const volumeSeries = volumeRef.current;
    if (!candleSeries || !volumeSeries) return;
    const map = new Map<number, Candle>();
    for (const bar of candles) {
      const time = toTime(bar.t);
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
    byTime.current = map;
  }, [candles]);

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
