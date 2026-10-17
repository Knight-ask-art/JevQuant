export function money(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return parsed.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function compactMoney(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return parsed.toLocaleString("en-US", {
    notation: Math.abs(parsed) >= 100000 ? "compact" : "standard",
    maximumFractionDigits: 2,
  });
}

export function pct(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return `${parsed.toFixed(digits)}%`;
}

export function signed(value: string | number | null | undefined, digits = 2): string {
  const parsed = Number(value ?? 0);
  if (!Number.isFinite(parsed)) return "—";
  const sign = parsed > 0 ? "+" : "";
  return `${sign}${money(parsed, digits)}`;
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "—";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function countdown(iso: string | null | undefined): string {
  if (!iso) return "—";
  const delta = new Date(iso).getTime() - Date.now();
  if (!Number.isFinite(delta)) return "—";
  if (delta <= 0) return "expired";
  const minutes = Math.floor(delta / 60000);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-GB", {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function shortId(value: string | null | undefined, length = 8): string {
  if (!value) return "—";
  return value.length <= length ? value : value.slice(0, length);
}

export function label(value: string | null | undefined): string {
  if (!value) return "—";
  return value
    .replace(/[_-]+/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export function toneForState(state: string): string {
  if (["FILLED", "POSITION_OPEN", "APPROVED", "CLOSED"].includes(state)) return "olive";
  if (["REJECTED", "RISK_REJECTED", "ERROR", "RECONCILIATION_REQUIRED"].includes(state)) {
    return "danger";
  }
  if (["ELIGIBLE", "SCORED", "CANDIDATE", "JEV_EVALUATING"].includes(state)) return "terracotta";
  if (["EXPIRED", "PARTIALLY_FILLED", "EXIT_PENDING"].includes(state)) return "yellow";
  return "";
}

export function isHealthy(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (value && typeof value === "object") {
    return Boolean((value as { healthy?: unknown }).healthy);
  }
  return false;
}
