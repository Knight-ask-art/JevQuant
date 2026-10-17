import type { CSSProperties, ReactNode } from "react";

import { ApiError } from "../lib/api";

export function PaperCard({
  children,
  title,
  eyebrow,
  actions,
  tint,
  style,
}: {
  children: ReactNode;
  title?: ReactNode;
  eyebrow?: ReactNode;
  actions?: ReactNode;
  tint?: "paper" | "olive" | "terracotta";
  style?: CSSProperties;
}) {
  return (
    <section className={`paper-card${tint ? ` tint-${tint}` : ""}`} style={style}>
      {(title || eyebrow || actions) && (
        <div className="card-head">
          <div>
            {eyebrow && <div className="eyebrow">{eyebrow}</div>}
            {title && <h2>{title}</h2>}
          </div>
          {actions && <div className="row">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function ScribbleUnderline({ width = 280 }: { width?: number }) {
  return (
    <svg
      className="scribble"
      viewBox={`0 0 ${width} 18`}
      aria-hidden="true"
      preserveAspectRatio="none"
    >
      <path
        d={`M2 12 Q${width * 0.18} 3 ${width * 0.34} 9 T${width * 0.66} 8 T${width - 2} 7`}
        fill="none"
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

export function Pill({
  children,
  tone = "",
  dot = false,
}: {
  children: ReactNode;
  tone?: "" | "olive" | "terracotta" | "danger" | "yellow" | "ink";
  dot?: boolean;
}) {
  return (
    <span className={`pill ${tone}`.trim()}>
      {dot && <span className="dot" />}
      {children}
    </span>
  );
}

export function Stat({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  tone?: "olive" | "terracotta" | "danger";
}) {
  const color =
    tone === "olive"
      ? "var(--olive)"
      : tone === "danger"
        ? "var(--danger)"
        : tone === "terracotta"
          ? "var(--terracotta)"
          : undefined;
  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ color }}>
        {value}
      </div>
      {note && <div className="stat-note">{note}</div>}
    </div>
  );
}

export function Meter({
  value,
  max,
  tone,
}: {
  value: number;
  max: number;
  tone?: "warn" | "danger";
}) {
  const ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  return (
    <div className={`meter${tone ? ` ${tone}` : ""}`}>
      <span style={{ width: `${ratio * 100}%` }} />
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="hand">{title}</div>
      {hint && (
        <p className="small" style={{ marginTop: 6 }}>
          {hint}
        </p>
      )}
      {action && <div style={{ marginTop: 12 }}>{action}</div>}
    </div>
  );
}

export function Loading({ what = "the notebook" }: { what?: string }) {
  return <div className="loading">Fetching {what}…</div>;
}

export function ErrorNote({ error }: { error: ApiError | null }) {
  if (!error) return null;
  return (
    <div className="callout danger">
      <strong>{error.code}</strong> — {error.message}
      {error.status === 401 && (
        <div className="small" style={{ marginTop: 4 }}>
          Add the operator API key under Audit &amp; Settings.
        </div>
      )}
    </div>
  );
}

export function Field({
  label: labelText,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="field">
      <span>{labelText}</span>
      {children}
      {hint && <span className="muted small">{hint}</span>}
    </label>
  );
}

export function InfoRow({ k, v }: { k: string; v: ReactNode }) {
  return (
    <>
      <dt>{k}</dt>
      <dd>{v}</dd>
    </>
  );
}
