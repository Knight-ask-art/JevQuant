import { useI18n } from "../i18n";
import type { SignalEvent } from "../lib/types";
import { clock, label, num } from "../lib/format";

function scale(values: number[], width: number, height: number, pad = 6) {
  if (!values.length) return { path: "", points: [] as Array<[number, number]> };
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const points = values.map((value, index) => {
    const x = pad + (index / Math.max(1, values.length - 1)) * (width - pad * 2);
    const y = pad + (1 - (value - min) / span) * (height - pad * 2);
    return [x, y] as [number, number];
  });
  const path = points
    .map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`)
    .join(" ");
  return { path, points };
}

export function Sparkline({
  values,
  width = 220,
  height = 48,
  color = "var(--olive)",
}: {
  values: number[];
  width?: number;
  height?: number;
  color?: string;
}) {
  const { t } = useI18n();
  const { path } = scale(values, width, height);
  if (!path) return null;
  return (
    <svg
      className="sketch-chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={t("chart.sparkline")}
    >
      <path
        d={path}
        fill="none"
        stroke={color}
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

export function EquityChart({
  points,
  height = 220,
}: {
  points: Array<{ at: string; equity_usdt: string }>;
  height?: number;
}) {
  const { t } = useI18n();
  const width = 760;
  const values = points.map((point) => Number(point.equity_usdt)).filter(Number.isFinite);
  if (values.length < 2) {
    return (
      <div className="empty hand" style={{ padding: 20 }}>
        {t("chart.notEnough")}
      </div>
    );
  }
  const { path, points: scaled } = scale(values, width, height, 10);
  const area = `${path} L${scaled[scaled.length - 1][0]} ${height - 10} L${scaled[0][0]} ${height - 10} Z`;
  const first = values[0];
  const last = values[values.length - 1];
  const tone = last >= first ? "var(--olive)" : "var(--danger)";
  return (
    <svg
      className="sketch-chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={t("chart.equityCurve")}
    >
      <g stroke="var(--line)" strokeDasharray="2 4" strokeWidth="1">
        {[0.25, 0.5, 0.75].map((fraction) => (
          <line key={fraction} x1="10" x2={width - 10} y1={height * fraction} y2={height * fraction} />
        ))}
      </g>
      <path d={area} fill={tone} opacity="0.08" />
      <path d={path} fill="none" stroke={tone} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={scaled[scaled.length - 1][0]} cy={scaled[scaled.length - 1][1]} r="3.4" fill={tone} />
      <text x="12" y="16" fontSize="11" fill="var(--muted)">
        {num(Math.max(...values))}
      </text>
      <text x="12" y={height - 4} fontSize="11" fill="var(--muted)">
        {num(Math.min(...values))}
      </text>
      <text x={width - 12} y="16" fontSize="11" fill="var(--muted)" textAnchor="end">
        {num(last)} USDT
      </text>
    </svg>
  );
}

export function ProbabilitySketch({
  buy,
  hold,
  sell,
  fit,
}: {
  buy: number;
  hold: number;
  sell: number;
  fit?: number;
}) {
  const { t } = useI18n();
  const width = 340;
  const height = 40;
  const dot = (value: number) => 10 + value * (width - 20);
  return (
    <svg
      className="prob-sketch"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={t("chart.probability")}
    >
      <line x1="10" x2={width - 10} y1={height / 2} y2={height / 2} stroke="var(--line-strong)" strokeDasharray="3 4" />
      <line
        x1={dot(sell)}
        x2={dot(buy)}
        y1={height / 2}
        y2={height / 2}
        stroke="var(--terracotta)"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <circle cx={dot(sell)} cy={height / 2} r="2.5" fill="var(--muted)" />
      <circle cx={dot(hold)} cy={height / 2} r="2" fill="var(--muted)" opacity="0.6" />
      <circle cx={dot(buy)} cy={height / 2} r="4" fill="var(--terracotta)" />
      <text x={dot(buy)} y={12} fontSize="10" fill="var(--ink-soft)" textAnchor="middle">
        {`${t("chart.buy")} ${buy.toFixed(2)}`}
      </text>
      {fit !== undefined && (
        <text x={dot(buy)} y={height - 3} fontSize="10" fill="var(--olive)" textAnchor="middle">
          {`${t("chart.fit")} ${fit.toFixed(2)}`}
        </text>
      )}
    </svg>
  );
}

export function LifecycleTrail({ events }: { events: SignalEvent[] }) {
  if (!events.length) return null;
  return (
    <div className="timeline">
      {events.map((event) => (
        <div className="timeline-item" key={event.id}>
          <span className="timeline-dot" />
          <div className="timeline-body">
            <div className="row" style={{ gap: 8 }}>
              <strong>{label(event.state)}</strong>
              <span className="muted small">{clock(event.created_at)}</span>
            </div>
            {Object.keys(event.detail || {}).length > 0 && (
              <div className="mono muted" style={{ marginTop: 2 }}>
                {JSON.stringify(event.detail)}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
