/** Locale used by the formatters. Set by the i18n provider on language change. */
let activeLocale = "en-US";

export function setFormatLocale(locale: string): void {
  activeLocale = locale;
}

function isChinese(): boolean {
  return activeLocale.toLowerCase().startsWith("zh");
}

export function money(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return parsed.toLocaleString(activeLocale, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function compactMoney(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return parsed.toLocaleString(activeLocale, {
    notation: Math.abs(parsed) >= 100000 ? "compact" : "standard",
    maximumFractionDigits: 2,
  });
}

/** Locale-aware plain number for chart axes and compact readouts. */
export function num(value: string | number | null | undefined, maxDigits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return parsed.toLocaleString(activeLocale, { maximumFractionDigits: maxDigits });
}

export function pct(value: string | number | null | undefined, digits = 2): string {  if (value === null || value === undefined || value === "") return "—";
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
  if (seconds < 5) return isChinese() ? "刚刚" : "just now";
  if (seconds < 60) return isChinese() ? `${seconds} 秒前` : `${seconds}s ago`;
  if (seconds < 3600)
    return isChinese() ? `${Math.round(seconds / 60)} 分钟前` : `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400)
    return isChinese() ? `${Math.round(seconds / 3600)} 小时前` : `${Math.round(seconds / 3600)}h ago`;
  return isChinese() ? `${Math.round(seconds / 86400)} 天前` : `${Math.round(seconds / 86400)}d ago`;
}

export function countdown(iso: string | null | undefined): string {
  if (!iso) return "—";
  const delta = new Date(iso).getTime() - Date.now();
  if (!Number.isFinite(delta)) return "—";
  if (delta <= 0) return isChinese() ? "已过期" : "expired";
  const minutes = Math.floor(delta / 60000);
  if (minutes < 60) return isChinese() ? `${minutes} 分钟` : `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return isChinese() ? `${hours} 小时 ${minutes % 60} 分` : `${hours}h ${minutes % 60}m`;
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(activeLocale, {
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

/**
 * Chinese labels for machine values. Keys are the raw values lowercased with
 * underscores intact; the title-case fallback stays in English for anything
 * not listed here (a backend field this UI has never seen).
 */
const ZH_LABELS: Record<string, string> = {
  // modes and venues
  paper: "模拟盘",
  shadow: "影子",
  testnet: "测试网",
  live: "实盘",
  binance_testnet: "Binance 测试网",
  binance_live: "Binance 实盘",
  hummingbot: "Hummingbot",
  synthetic: "合成",
  binance: "Binance",
  // sides, types
  buy: "买入",
  sell: "卖出",
  limit: "限价",
  market: "市价",
  open: "开仓",
  close: "平仓",
  // signal states
  candidate: "候选",
  jev_evaluating: "评估中",
  scored: "已评分",
  rejected: "已拒绝",
  eligible: "可入场",
  intent_created: "已生成意图",
  risk_pending: "风控待审",
  risk_rejected: "风控拒绝",
  approved: "已批准",
  execution_pending: "执行待发",
  accepted: "已接受",
  partially_filled: "部分成交",
  filled: "已成交",
  cancelled: "已取消",
  position_open: "持仓中",
  exit_pending: "退出待发",
  closed: "已平仓",
  expired: "已过期",
  error: "错误",
  reconciliation_required: "需要对账",
  // order / position statuses
  pending: "待处理",
  unknown: "未知",
  shadowed: "影子记录",
  // policy statuses
  active: "生效",
  invalidated: "已失效",
  superseded: "已替代",
  // arming
  disarmed: "未武装",
  preflight_running: "预检中",
  preflight_failed: "预检失败",
  armed: "已武装",
  // regimes
  risk_on: "风险偏好",
  risk_off: "风险规避",
  trend: "趋势",
  range: "震荡",
  high_vol: "高波动",
  uncertain: "不确定",
  // Jev choices
  wait: "观望",
  // backtest runs
  running: "运行中",
  completed: "已完成",
  succeeded: "成功",
  failed: "失败",
  queued: "排队",
  // dependencies and routes
  database: "数据库",
  execution: "执行",
  policy: "策略",
  redis: "Redis",
  jev: "Jev",
  // reconciliation outcomes
  cooldown: "冷却期",
  unavailable: "不可用",
  incomplete: "不完整",
  healthy: "正常",
  // reasons that block entries
  kill_switch: "紧急停止",
  policy_unavailable: "策略不可用",
  policy_missing: "策略缺失",
  market_feed_unhealthy: "行情源不健康",
  execution_unhealthy: "执行不健康",
  stale_market: "行情过期",
  // audit / stream event names
  cycle_completed: "周期完成",
  order_filled: "订单成交",
  mode_changed: "模式切换",
  signal_created: "信号创建",
  policy_compiled: "策略已编译",
  fill_recorded: "成交已记录",
  position_opened: "持仓已开",
  position_closed: "持仓已平",
  // configuration keys
  app_env: "运行环境",
  data_source: "数据源",
  execution_backend: "执行后端",
  synthetic_market: "合成行情",
  simulated_models: "模拟模型",
  auto_cycle_seconds: "自动周期秒数",
  auth_disabled: "关闭认证",
  balance_sync_seconds: "余额同步秒数",
  balance_auto_reconcile: "漂移自动对账",
  balance_reconcile_cooldown_seconds: "对账冷却秒数",
  max_position_notional_usdt: "单笔最大名义金额",
  max_gross_exposure_pct: "最大总敞口",
  max_daily_loss_pct: "最大当日亏损",
  max_positions: "最大持仓数",
  max_drawdown_pct: "最大回撤",
  allowed_symbols: "允许标的",
  require_withdrawals_disabled: "要求禁用提现",
  require_ip_restrict: "要求 IP 限制",
  entry_filter_ttl_seconds: "入场过滤器 TTL",
  trading_mode: "交易模式",
  confirm_live: "实盘确认",
};

export function label(value: string | null | undefined): string {
  if (!value) return "—";
  if (isChinese()) {
    const translated = ZH_LABELS[value.toLowerCase()];
    if (translated) return translated;
  }
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
