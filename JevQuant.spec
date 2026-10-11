# JevQuant machine-readable product + engineering specification
# Format: YAML 1.2-compatible; extension intentionally .spec for coding-agent handoff.
spec_version: "1.0.0"
project:
  name: JevQuant
  subtitle: "Jev + Binance Spot 轻量级 AI 量化机器人"
  created_at: "2026-10-10"
  locale: zh-CN
  owner_profile: "Python 初学者；Windows 本地开发；计划借助编码 Agent 实现"
  stage: "requirements-approved / not-implemented"
  purpose: "学习量化研发与小额自动化现货交易，重点保证可审计、可模拟和资金风险可控；不承诺盈利。"

priority_rules:
  - "安全与资金约束 > 交易执行正确性 > 数据可靠性 > 模型信号 > UI 体验"
  - "任何真实交易功能必须在 paper、testnet 验收通过后才允许启用。"
  - "不能将 Jev 的判断概率视作经金融市场校准的胜率。"
  - "运行环境/账户不允许使用 Binance 服务时，停止实盘开发和启用。"

scope:
  release: "MVP v0.1"
  include:
    - "Binance Spot BTC/USDT 单交易对；只做现货买入、卖出或观望"
    - "读取已收盘 1h K 线、最新行情和 exchangeInfo 交易过滤器"
    - "计算 SMA20、SMA60、RSI14、ATR14 与成交量变化"
    - "TypeSafe Jev 模型 Choice 决策：BUY / SELL / HOLD"
    - "独立基础规则策略作对照（如 SMA20/SMA60），可关闭 Jev"
    - "独立风控与模拟账本；真实手续费和滑点假设"
    - "Binance Spot Testnet 执行适配层"
    - "默认禁用的 Binance Spot 实盘适配层"
    - "SQLite 交易、信号、订单和故障审计日志"
    - "Streamlit 本地只读行情与交易监控、暂停新开仓控制"
    - "pytest 自动化测试与重启恢复"
  exclude:
    - "合约、杠杆、借贷、网格、马丁、加仓、做空"
    - "跨交易所套利、高频交易、多币种组合、社交交易"
    - "任何自动充值/兑换/提现功能"
    - "自主 Agent 修改资金风控或授予自身实盘权限"
    - "盈利保证、保本承诺或未经验证的收益预测"
    - "在 MVP 中引入 Vue、FastAPI、MySQL、容器编排"

nonnegotiable_limits:
  trading_venue: "Binance Spot（非 Binance.US；须确认所在地区合法且账户可用）"
  allowed_symbol: "BTC/USDT"
  timeframe: "1h"
  max_open_positions: 1
  max_new_entries_per_utc_day: 1
  account_budget:
    user_funding_cap_cny: 50
    interpretation: "50 元人民币为用户计划存入币安的全部实盘本金；非收益目标、非自动充值指令。"
    startup: "用户手动确定换得的 USDT 数量，锁定为 initial_capital_usdt；不得超额交易。"
    no_auto_fx_or_deposits: true
    available_balance_ceiling: true
    api_and_infrastructure_fees: "另行显示成本估算；未获用户同意不得自动充值或增加付费项目。"
  leverage: 1
  margin: false
  withdrawals: false
  transfer_assets: false
  fail_closed: true
  default_mode: "paper"

technology:
  python: ">=3.10,<3.14（推荐 Python 3.11 或 3.12；实际按锁定依赖验证）"
  exchange_adapter: "ccxt (Binance Spot only)"
  model_sdk: "typesafe-sdk (官方 TypeSafe AI Python SDK)"
  database: "SQLite + Python sqlite3"
  indicators: "pandas（公式单测覆盖）"
  dashboard: "streamlit"
  tests: "pytest + unittest.mock"
  money: "decimal.Decimal；拒绝使用二进制 float 做额度/订单精度的业务计算"
  secrets: "环境变量/.env（不提交仓库、不写日志、不通过 Streamlit 展示）"

run_modes:
  - name: paper
    description: "默认；使用真实公开行情、模拟余额及模拟订单；不需要 Binance 私有 API Key。"
    private_exchange_api: false
    live_orders_allowed: false
  - name: testnet
    description: "仅使用 Binance Spot Testnet 的隔离 API Key；模拟资产可能被周期性重置。"
    private_exchange_api: true
    live_orders_allowed: false
  - name: live
    description: "真实币安现货订单；所有准入检查通过，必须显式人工启用。"
    private_exchange_api: true
    live_orders_allowed: true
    activation_requirements:
      - "TRADING_MODE=live"
      - "ENABLE_LIVE_TRADING=true"
      - "CLI 显式 --confirm-live；禁止通过环境变量单独完成最终确认"
      - "preflight 检查全部通过（账户、余额、符号、风控、交易过滤器、权限）"
      - "具备暂停新开仓与故障熔断"

model_contract:
  provider: "TypeSafe AI System One / Jev"
  model: "jev-latest（可配置，版本记录到日志）"
  api_key_env: "TYPESAFE_API_KEY"
  engine: "TypeSafeClient.system_one，Choice(instructions=..., criteria=...)"
  input:
    only_closed_candles: true
    fields: [symbol, candle_close_utc, timeframe, closes, volumes, sma20, sma60, rsi14, atr14, price_change_24h, volatility, has_position]
    market_data_limit: "仅包含已确认时间戳的数据，禁止未来数据泄漏。"
  output:
    enum: [BUY, SELL, HOLD]
    raw_response_saved: "脱敏后 JSON；留存模型版本、状态摘要与请求时间"
    default_on_error: HOLD
    missing_or_invalid_response: HOLD
    timeout_behavior: "本周期不产生新开仓信号；对已有仓位仍执行独立风控"
  guardrail: "Jev 不得决定订单金额、交易模式、交易所账户权限或撤销风控规则。"
  evaluation: "须与不调用 Jev 的简单基线比较；记录含手续费净值，不把 AI 置信度直接作为胜率。"
  rate_policy:
    call_frequency: "每根完整 1h K 线至多调用一次"
    max_calls_per_utc_day: 30
    max_monthly_cost_usd: "配置项；未配置付费限额则阻止付费模型调用"

market_rules:
  fetch_before_paper_and_live: "exchange.load_markets(); 原始 exchangeInfo filters 需保存快照"
  required_filters:
    - "status == TRADING"
    - "SPOT trading permission enabled"
    - "MIN_NOTIONAL 或 NOTIONAL，确认 market-order 适用条件"
    - "LOT_SIZE, MARKET_LOT_SIZE, PRICE_FILTER（按实际订单类型及适用性）"
    - "min/max quantity, stepSize, 价格精度、最小名义金额"
  no_hardcoded_exchange_minimum: true
  notional_rules_changeable: true
  initial_min_notional_reference: "曾观察到 BTC/USDT 为 5 USDT；仅供测试场景，不可当作固定运行常量。"
  reject_when:
    - "不能查询到最新过滤器/交易对被暂停/权限不匹配"
    - "可用 USDT 不足最低金额 + 手续费和必要缓冲"
    - "按数量步长向下取整后不满足订单条件"
    - "买入后在假设跌幅及手续费下，预计卖单不足最小交易金额（Exit feasibility）"
    - "余额/行情/过滤器已经过期"
    - "买卖侧订单被交易所限制或已有未完成订单"
  exit_feasibility:
    reference_drawdown: "0.03（仅模拟保守场景；并非保证止损能以此成交）"
    include: [entry_fee, exit_fee, quantity_rounding, configurable_slippage_buffer, adverse_price_move]
    fail_action: "不予开仓，并记录 REJECT_EXIT_MIN_NOTIONAL"
    existing_position_below_minimum: "告警并停止新开仓；不发送注定被拒绝的订单；提示需要人工处理"

risk_policy:
  spot_only: true
  no_repeated_entry_while_long: true
  max_position_count: 1
  opening_allocation: "不得超出 initial_capital_usdt 与当前可用 USDT；应为费用与可能的出场留足缓冲"
  reference_stop_loss_pct: "0.03（仅作为初始策略参数，不保证最大亏损）"
  daily_loss_halt_pct_initial_equity: "0.03（达到/超出时暂停新开仓并提示人工检查）"
  feed_max_staleness_seconds: 180
  no_new_entry_if_unreconciled_orders: true
  no_new_entry_if_service_unhealthy: true
  fail_close_on_unknown_balance: true
  circuit_breakers:
    - repeated_api_errors
    - data_stale
    - exchange_timestamp_drift
    - inconsistent_position_or_order_state
    - daily_loss_limit_reached
    - stop_exit_rejected_or_below_minimum
    - manual_halt
  halt_semantics: "停止新开仓；对已有仓位仍运行状态查询及允许的风险退出，无法退出时告警并交由人工处理。"
  stop_policy: "任何软件止损不能保证成交价和固定最大损失；实盘必须记录交易所订单状态及已成交数量。"

order_lifecycle:
  states: [FLAT, ENTRY_SUBMITTING, LONG, EXIT_SUBMITTING, HALTED, RECONCILIATION_REQUIRED]
  allowed_flow:
    - "FLAT -> ENTRY_SUBMITTING -> LONG -> EXIT_SUBMITTING -> FLAT"
    - "任一状态 -> HALTED（停止新开仓）"
    - "订单超时或未知执行结果 -> RECONCILIATION_REQUIRED -> 查询后恢复"
  uniqueness: "每个信号生成稳定 signal_id（symbol + closed_candle_time + strategy_version + direction）"
  idempotency: "每次交易客户端 order id 必须唯一且可重试查询；不因 HTTP 超时直接重发同一交易意图。"
  reconciliation:
    - "每次启动先读取数据库订单状态，并查询交易所未完成订单和实际余额"
    - "未确认状态禁止新开仓；查询而非盲目重复 create_order"
    - "按实际成交回报、手续费与残余余额更新持仓"
  timestamp_policy: "内部统一 UTC；时区仅在 UI 展示层处理"

contracts:
  MarketSnapshot:
    required: [symbol, candle_close_utc, ohlcv, indicators, market_rules_version, acquired_at_utc]
  TradeSignal:
    required: [signal_id, model_name, model_version, candle_close_utc, action, timestamp_utc, reason_code]
    action_enum: [BUY, SELL, HOLD]
  RiskDecision:
    required: [signal_id, approved, reason_code, max_notional_usdt, checks]
  OrderIntent:
    required: [client_order_id, signal_id, mode, symbol, side, order_type, quantity_or_quote_budget]
  OrderReport:
    required: [client_order_id, exchange_order_id, status, filled_base_qty, spent_quote, fees, timestamp_utc]

storage:
  tables:
    - candles
    - market_rules_snapshots
    - model_signals
    - risk_checks
    - order_intents
    - exchange_orders
    - fills
    - positions
    - balances
    - equity_snapshots
    - incidents
    - settings_audit
  constraints:
    - "signal_id 唯一约束"
    - "client_order_id 唯一约束"
    - "资金和数量以 Decimal 字符串保存"
    - "禁止记录 API Key、Secret、原始请求 Authorization 头"
    - "数据库事务提交成功后才调用下游动作；意图与结果分别审计"
    - "重启恢复必须可处理部分成交、未成交、取消和未知订单"

configuration:
  example_env:
    TRADING_MODE: paper
    ENABLE_LIVE_TRADING: "false"
    EXCHANGE: binance
    SYMBOL: BTC/USDT
    TIMEFRAME: 1h
    PAPER_INITIAL_BALANCE_USDT: "7.00"
    LIVE_INITIAL_CAPITAL_USDT: "" # 手工输入，不从模拟余额复制
    TYPESAFE_API_KEY: "" # 本地存储；代码中不写入真实值
    BINANCE_API_KEY: ""
    BINANCE_API_SECRET: ""
    BINANCE_TESTNET_API_KEY: ""
    BINANCE_TESTNET_API_SECRET: ""
    JEV_MODEL: jev-latest
    JEV_MAX_MONTHLY_COST_USD: "0.50"
    DASHBOARD_HOST: "127.0.0.1"
  validators:
    - "live 模式不能使用 Testnet API Key；反之亦然"
    - "live 与 testnet 用不同密钥、端点、数据目录；禁止自动回退端点"
    - "未知配置、不合理负数、不完整密钥必须拒绝启动"
    - "初始实盘金额需人工按实际 50 元等值 USDT 确认"

architecture:
  root_package: jevquant
  modules:
    - path: "src/jevquant/config.py"
      duty: "模式配置、输入校验、可用交易权限门禁"
    - path: "src/jevquant/exchange.py"
      duty: "公开行情、交易过滤器、可替换的 paper/testnet/live 交易适配器"
    - path: "src/jevquant/indicators.py"
      duty: "从已收盘 K 线计算指标（无未来函数）"
    - path: "src/jevquant/jev_engine.py"
      duty: "TypeSafe SDK 封装、响应校验、异常时 HOLD"
    - path: "src/jevquant/strategy.py"
      duty: "策略信号组合与简单对照基线"
    - path: "src/jevquant/risk.py"
      duty: "所有资金与交易规则、出场可行性判断、熔断"
    - path: "src/jevquant/orders.py"
      duty: "订单状态机、去重、查询对账、部分成交处理"
    - path: "src/jevquant/storage.py"
      duty: "SQLite 迁移、事务、审计日志"
    - path: "src/jevquant/runner.py"
      duty: "每根收盘K线最多执行一次；启动恢复与停机控制"
    - path: "dashboard/app.py"
      duty: "Streamlit 监控，默认绑定本机，只读加禁新开仓控制"
    - path: "tests/"
      duty: "单元/集成/回放/失效测试"

release_gates:
  phase_1_market_data:
    deliverables: ["能显示 BTC/USDT 最新价格与 UTC K 线", "提取并持久化交易过滤器", "不需要真实密钥"]
  phase_2_ai_and_backtest:
    deliverables: ["TypeSafe 官方 SDK 单次调用成功", "无 Key 时可运行基线与 Mock", "拒绝未来数据", "结果可复现"]
  phase_3_paper:
    deliverables: ["虚拟账户订单成交与记账", "手续费/滑点/精度模拟", "亏损熔断", "止损卖出可行性验证"]
    observation: "至少 7–14 天观察作为操作稳定性测试，不等于证明策略能盈利"
  phase_4_testnet:
    deliverables: ["隔离测试网密钥", "真实 API 的 create/cancel/query 状态一致性", "重启恢复", "故障注入通过"]
  phase_5_live:
    prerequisite: "全部验收项通过，并由用户针对实盘再次明确授权"
    deliverables: ["显式实盘开关", "手工确定本金上限", "实际账户权限和最小下单额复查", "可暂停新开仓"]

acceptance_tests:
  - {id: T01, given: "无私有密钥且 paper 模式", when: "启动", then: "成功运行公开行情，不可能创建实盘订单"}
  - {id: T02, given: "同一根已收盘 1h K 线", when: "调度器重启两次", then: "每策略最多生成一个 signal_id"}
  - {id: T03, given: "Jev 无响应或请求失败", when: "执行分析", then: "HOLD 并留审计；不开新仓"}
  - {id: T04, given: "行情时间戳过期", when: "试图 BUY", then: "被风控拒绝并熔断"}
  - {id: T05, given: "BTC/USDT 最小名义额高于可用 USDT", when: "试图 BUY", then: "拒单且不调用交易 API"}
  - {id: T06, given: "买入通过但跌幅3%后卖出价值低于门槛", when: "入场前预检", then: "拒绝入场，记录 REJECT_EXIT_MIN_NOTIONAL"}
  - {id: T07, given: "数量不符合步长", when: "计算订单", then: "使用 Decimal 按交易所规则向下处理并重新校验"}
  - {id: T08, given: "手续费以 BTC 或 BNB 等资产扣取", when: "成交回报到达", then: "更新真实可用持仓，费用折算仅用于收益统计"}
  - {id: T09, given: "存在未确认订单", when: "收到 BUY 信号", then: "不得重复下单，优先对账"}
  - {id: T10, given: "订单请求超时但可能成交", when: "恢复连接", then: "按 client_order_id 查询，不盲目再次提交"}
  - {id: T11, given: "订单部分成交", when: "撤单或重启", then: "已成交部分计入持仓与手续费"}
  - {id: T12, given: "每日亏损阈值触发", when: "新 BUY 信号", then: "停止新开仓；保留告警与仓位风险管理"}
  - {id: T13, given: "账户余额无法读取", when: "需要实盘买入", then: "拒绝发单并告警"}
  - {id: T14, given: "TRADING_MODE=live 但 ENABLE_LIVE_TRADING=false", when: "启动", then: "拒绝开启实盘订单引擎"}
  - {id: T15, given: "live 两个开关为真，但没有 --confirm-live", when: "启动", then: "拒绝实盘"}
  - {id: T16, given: "未授权的地区或账户不支持现货交易", when: "实盘 preflight", then: "拒绝实盘"}
  - {id: T17, given: "测试网发生周期重置", when: "订单和余额对账", then: "检测状态异常、熔断并重新建立测试基线"}
  - {id: T18, given: "禁新开仓开关触发", when: "BUY 与风险退出同时发生", then: "拒绝 BUY；可继续执行经风控批准的退出/查询"}
  - {id: T19, given: "未配置 Jev 费用上限", when: "拟调用付费 SDK", then: "不调用并标记成本配置缺失"}
  - {id: T20, given: "日志与界面导出", when: "扫描敏感字段", then: "无 API Secret、Authorization 或原始凭证"}

agent_instructions:
  implementation_order: [config, storage, market_data, indicators, jev_adapter, risk, paper, order_state_machine, testnet, dashboard, live_gate]
  code_quality:
    - "先提交每个模块的接口和单元测试，再实现业务逻辑。"
    - "所有交易操作通过 ExchangeAdapter 接口，策略层严禁调用 CCXT.create_order。"
    - "没有真实授权的情况下，禁止调用 live create_order，禁止请求或输出用户密钥。"
    - "每阶段交付 README、运行示例、pytest 结果和当前未解决的风险。"
    - "除非明确同意，禁止新增云服务器、付费数据源、自动更新代理和远程控制服务。"
  delivery_definition:
    - "无密钥可跑 paper 演示，模拟数据标注清楚。"
    - "实盘必须多重显式解锁且具备明确的阻断/回退行为。"
    - "pytest 全通过；可复现实盘前故障和小额订单门槛测试。"
    - "文档明确区分已实现、仅设计和未验证功能。"

references:
  - "https://github.com/typesafe-ai/typesafe-sdk-python"
  - "https://docs.typesafe.ai/sdk/python/"
  - "https://github.com/binance/binance-spot-api-docs"
  - "https://github.com/binance/binance-spot-api-docs/blob/master/filters.md"
  - "https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md"
  - "https://github.com/ccxt/ccxt"
  - "https://docs.ccxt.com/docs/exchanges/binance"
  - "https://github.com/freqtrade/freqtrade"
