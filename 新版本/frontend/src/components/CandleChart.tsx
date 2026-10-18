import type { Candle } from "../lib/types";

/**
 * A candlestick chart with a volume strip and a dashed last-price line.
 *
 * Drawn as plain SVG, matching the rest of the dashboard: no chart library,
 * no animation, no interpolation. Only closed candles are passed in, so the
 * right edge is the last completed bar rather than a moving one.
 */
export function CandleChart({
  candles,
  height = 260,
  upColor = "var(--olive)",
  downColor = "var(--danger)",
  ariaLabel,
  formatPrice,
  formatVolume,
}: {
  candles: Candle[];
  height?: number;
  upColor?: string;
  downColor?: string;
  ariaLabel?: string;
  formatPrice: (value: number) => string;
  formatVolume: (value: number) => string;
}) {
  const width = 760;
  const padLeft = 8;
  const padRight = 78;
  const padTop = 10;
  const priceHeight = Math.round((height - padTop) * 0.76);
  const volumeTop = padTop + priceHeight + 14;
  const volumeHeight = height - volumeTop - 6;
  const plotWidth = width - padLeft - padRight;

  if (candles.length < 2) return null;

  const highs = candles.map((bar) => bar.h);
  const lows = candles.map((bar) => bar.l);
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const span = max - min || 1;
  const maxVolume = Math.max(...candles.map((bar) => bar.v), 1);
  const slot = plotWidth / candles.length;
  const bodyWidth = Math.max(1.5, Math.min(12, slot * 0.62));

  const y = (price: number) =>
    padTop + (1 - (price - min) / span) * (priceHeight - padTop);
  const x = (index: number) => padLeft + slot * index + slot / 2;

  const last = candles[candles.length - 1];
  const lastUp = last.c >= last.o;
  const lastTone = lastUp ? upColor : downColor;

  // Four horizontal gridlines at round-ish fractions of the range.
  const gridlines = [0, 0.25, 0.5, 0.75, 1].map((fraction) => {
    const price = min + span * fraction;
    return { price, y: y(price) };
  });

  return (
    <svg
      className="candle-chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={ariaLabel ?? "candlestick chart"}
    >
      <g stroke="var(--line)" strokeDasharray="2 4" strokeWidth="1">
        {gridlines.map((line) => (
          <line key={line.y} x1={padLeft} x2={padLeft + plotWidth} y1={line.y} y2={line.y} />
        ))}
      </g>
      {gridlines.map((line) => (
        <text
          key={`label-${line.y}`}
          x={width - padRight + 6}
          y={line.y + 3}
          fontSize="10"
          fill="var(--muted)"
        >
          {formatPrice(line.price)}
        </text>
      ))}

      <line
        x1={padLeft}
        x2={padLeft + plotWidth}
        y1={y(last.c)}
        y2={y(last.c)}
        stroke={lastTone}
        strokeDasharray="4 3"
        strokeWidth="1"
        opacity="0.55"
      />
      <rect
        x={width - padRight + 2}
        y={y(last.c) - 8}
        width={padRight - 6}
        height={16}
        rx={3}
        fill={lastTone}
        opacity="0.12"
      />
      <text
        x={width - padRight + 6}
        y={y(last.c) + 3.5}
        fontSize="10"
        fontWeight="600"
        fill={lastTone}
      >
        {formatPrice(last.c)}
      </text>

      {candles.map((bar, index) => {
        const tone = bar.c >= bar.o ? upColor : downColor;
        const openY = y(bar.o);
        const closeY = y(bar.c);
        const top = Math.min(openY, closeY);
        const body = Math.max(1.2, Math.abs(closeY - openY));
        const volumeHeightBar = (bar.v / maxVolume) * volumeHeight;
        return (
          <g key={bar.t}>
            <line
              x1={x(index)}
              x2={x(index)}
              y1={y(bar.h)}
              y2={y(bar.l)}
              stroke={tone}
              strokeWidth="1"
            />
            <rect
              x={x(index) - bodyWidth / 2}
              y={top}
              width={bodyWidth}
              height={body}
              fill={bar.c >= bar.o ? tone : "var(--card)"}
              stroke={tone}
              strokeWidth="1"
            />
            <rect
              x={x(index) - bodyWidth / 2}
              y={volumeTop + (volumeHeight - volumeHeightBar)}
              width={bodyWidth}
              height={Math.max(0.5, volumeHeightBar)}
              fill={tone}
              opacity="0.28"
            />
          </g>
        );
      })}

      <text x={padLeft} y={volumeTop - 4} fontSize="9" fill="var(--muted)">
        {formatVolume(maxVolume)}
      </text>
      <text x={padLeft + plotWidth} y={volumeTop - 4} fontSize="9" fill="var(--muted)" textAnchor="end">
        {formatVolume(0)}
      </text>
    </svg>
  );
}
