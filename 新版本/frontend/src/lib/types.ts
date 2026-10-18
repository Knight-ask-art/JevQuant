export type ModeName = "paper" | "shadow" | "testnet" | "live";

export interface DependencyHealth {
  healthy?: boolean;
  [key: string]: unknown;
}

export interface ModeStatus {
  mode: ModeName;
  live_armed: boolean;
  arming_status: string;
  armed_at: string | null;
  arming_expires_at: string | null;
  kill_switch: boolean;
  last_reason: string | null;
  live_config_allowed: boolean;
  execution_backend: string;
}

export interface AccountView {
  id: string;
  equity_usdt: string;
  available_quote_usdt: string;
  gross_exposure_usdt: string;
  open_positions: number;
  daily_realized_pnl_usdt: string;
  daily_realized_pnl_pct: string;
  drawdown_pct: string;
}

export interface PositionView {
  id: string;
  symbol: string;
  qty: string;
  avg_entry_price: string;
  mark_price?: string;
  unrealized_pnl_usdt?: string;
  stop_loss_bps: string;
  take_profit_bps: string;
  max_holding_seconds: number;
  status: string;
  opened_at: string;
  is_simulated: boolean;
  realized_pnl_usdt?: string;
}

export interface PolicyView {
  policy_id: string;
  status: string;
  source: string;
  simulated: boolean;
  policy_hash: string;
  generated_at: string;
  expires_at: string;
  raw: Record<string, unknown>;
  effective: Record<string, unknown>;
  clamps: Array<{
    field: string;
    requested: string;
    effective: string;
    reason: string;
  }>;
}

export interface ModelConnection {
  provider: string;
  configured: boolean;
  api_key_configured: boolean;
  base_url: string | null;
  model: string | null;
  simulated: boolean;
  protocol?: string;
  path?: string;
}

export interface AutomationStatus {
  enabled: boolean;
  interval_seconds: number;
  next_run_at: string | null;
  running: boolean;
}

export interface SystemStatus {
  service: string;
  app_env: string;
  server_time: string;
  mode: ModeStatus;
  dependencies: Record<string, DependencyHealth>;
  data_source: string;
  synthetic_market: boolean;
  simulated_models: boolean;
  account: AccountView;
  positions: PositionView[];
  policy: (PolicyView & { mode_scope: string[] }) | null;
  server_limits: {
    max_position_notional_usdt: string;
    max_gross_exposure_pct: string;
    max_daily_loss_pct: string;
    max_positions: number;
    max_drawdown_pct: string;
    allowed_symbols: string[];
  };
  execution_backend: string;
  automation: AutomationStatus;
  models: {
    llm: ModelConnection;
    jev: ModelConnection;
  };
}

export type SignalState =
  | "CANDIDATE"
  | "JEV_EVALUATING"
  | "SCORED"
  | "ELIGIBLE"
  | "INTENT_CREATED"
  | "RISK_PENDING"
  | "APPROVED"
  | "EXECUTION_PENDING"
  | "ACCEPTED"
  | "PARTIALLY_FILLED"
  | "FILLED"
  | "POSITION_OPEN"
  | "EXIT_PENDING"
  | "CLOSED"
  | "REJECTED"
  | "RISK_REJECTED"
  | "EXPIRED"
  | "ERROR"
  | "RECONCILIATION_REQUIRED";

export interface SignalView {
  id: string;
  symbol: string;
  state: SignalState;
  side: string;
  policy_id: string;
  snapshot_id: string;
  jev_evaluation_id: string | null;
  score: Record<string, unknown>;
  reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface SignalEvent {
  id: string;
  signal_id: string;
  state: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface OrderView {
  id: string;
  intent_id: string;
  exchange_order_id: string | null;
  symbol: string;
  side: string;
  order_type: string;
  qty: string;
  price: string | null;
  status: string;
  filled_qty: string;
  avg_fill_price: string;
  fee_quote: string;
  mode: string;
  is_simulated: boolean;
  created_at: string;
  updated_at: string;
}

export interface PnlPoint {
  id: string;
  as_of: string;
  equity_usdt: string;
  cash_quote_usdt: string;
  realized_pnl_usdt: string;
  unrealized_pnl_usdt: string;
  gross_exposure_usdt: string;
  daily_realized_pnl_usdt: string;
  drawdown_pct: string;
}

export interface AuditEvent {
  seq: number;
  id: string;
  event_type: string;
  entity_type: string;
  entity_id: string | null;
  payload: Record<string, unknown>;
  prev_hash: string;
  event_hash: string;
  created_at: string;
}

export interface BacktestRun {
  id: string;
  status: string;
  request: Record<string, unknown>;
  result: BacktestReport | Record<string, never>;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface BacktestReport {
  engine: string;
  is_synthetic: boolean;
  generated_at: string;
  spec: Record<string, unknown>;
  summary: {
    trades: number;
    wins: number;
    losses: number;
    win_rate_pct: string;
    net_pnl_usdt: string;
    max_drawdown_pct: string;
    symbols: string[];
  };
  per_symbol: Record<
    string,
    {
      trades: number;
      win_rate_pct: string;
      net_pnl_usdt: string;
      ending_equity_usdt: string;
      return_pct: string;
      max_drawdown_pct: string;
      equity_curve: Array<{ at: string; equity_usdt: string }>;
    }
  >;
  trades: Array<Record<string, string>>;
  assumptions: string[];
}

export interface CycleResult {
  cycle_id: string;
  mode: string;
  policy_generated: boolean;
  entries_blocked_reason: string | null;
  signals_created: number;
  signals_eligible: number;
  intents_created: number;
  approved: number;
  rejected: number;
  executed: number;
  exits: number;
  errors: number;
  equity_usdt: string;
  open_positions: number;
  simulated: boolean;
  notes: string[];
}
