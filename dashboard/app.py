import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from jevquant.config import AppConfig, load_config_from_env
from jevquant.exchange import BinancePublicAdapter
from jevquant.indicators import compute_indicators
from jevquant.jev_engine import JevEngine
from jevquant.orders import OrderStateMachine, PaperAccount
from jevquant.presentation import format_confidence, format_latency_ms
from jevquant.risk import RiskManager
from jevquant.strategy import JevAIStrategy, SmaBaselineStrategy

# Page setup without emoji
st.set_page_config(
    page_title="JevQuant - Jev Market Reflex",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Initialize Session State
if "lang" not in st.session_state:
    st.session_state.lang = "zh"  # default to Chinese

if "nav_tab" not in st.session_state:
    st.session_state.nav_tab = "monitor"

if "paper_account" not in st.session_state:
    st.session_state.paper_account = PaperAccount(
        initial_usdt=Decimal("7.00"),
        maker_taker_fee=Decimal("0.001"),
        slippage_pct=Decimal("0.0005"),
    )

if "risk_manager" not in st.session_state:
    st.session_state.risk_manager = RiskManager(
        initial_capital_usdt=Decimal("7.00"),
        max_daily_entries=1,
        daily_loss_halt_pct=Decimal("0.03"),
    )

if "order_sm" not in st.session_state:
    st.session_state.order_sm = OrderStateMachine()

if "proxy_check_result" not in st.session_state:
    st.session_state.proxy_check_result = None

# Load persistent or runtime configuration
cfg = load_config_from_env()
paper = st.session_state.paper_account
risk = st.session_state.risk_manager
order_sm = st.session_state.order_sm

# i18n Dictionary
I18N = {
    "zh": {
        "title": "JEV 量化决策引擎",
        "subtitle": "基于 TYPESAFE JEV + 币安现货 · 无未来函数校验 · 独立风控闭环",
        "nav_monitor": "决策监控大屏",
        "nav_ledger": "模拟账本与持仓",
        "nav_settings": "系统与风控设置",
        "nav_docs": "架构与安全规则",
        "demo_banner": "[演示模式] 当前页面显示的是合成行情与示例数据，不代表实时报价或模型绩效。暂停按钮只影响本网页会话，不控制后台交易进程。",
        "beginner_guide_title": "第一次使用？了解页面上的数字和按钮",
        "beginner_guide_body": "这是一个无真实订单的示例界面。BUY / SELL / HOLD 分别表示建议买入、建议卖出和观望，不是直接下单指令。置信度显示“—”表示模型没有给出可信的数值，绝不自动补成 65%。Paper 余额是当前网页会话内的模拟数据；这里的按钮无法暂停其他进程。建议先阅读“架构与安全规则”，再考虑测试网。",
        "mode_preview_notice": "交易模式选项仅用于展示，本页保存操作不会启动测试网、启用实盘或修改 .env 配置。",
        "kpi_decisions": "累计决策次数",
        "kpi_avg_latency": "平均响应延迟",
        "kpi_p95_latency": "P95 峰值延迟",
        "kpi_sample_rate": "指标采样周期",
        "kpi_sample_rate_val": "1小时 / 收盘K线",
        "sec_snapshots": "合成行情快照 (示例 1H K线)",
        "sec_decision_out": "AI 与基线决策输出",
        "sec_causal_chain": "决策因果链 (LATEST CAUSAL CHAIN)",
        "sec_action_log": "示例动作日志（非实时）",
        "radar_title": "演示决策核心",
        "radar_sub": "结构化 CHOICE 引擎",
        "card_price": "最新价格",
        "card_indicators": "指标快照",
        "decision_reason": "判定依据",
        "signal_id": "唯一信号指纹",
        "baseline_title": "SMA 双均线基线 (对照策略)",
        "baseline_sub": "零 API 成本基线规则",
        "portfolio_title": "模拟账户资产 (PAPER)",
        "portfolio_equity": "总资产折合",
        "portfolio_free": "可用 USDT",
        "portfolio_btc": "持有 BTC",
        "portfolio_budget": "初始规划预算",
        "heartbeat_title": "决策心跳脉冲",
        "heartbeat_meta": "演示模式：未采集持续执行统计",
        "risk_status": "风控审批状态",
        "exit_feasibility": "逆向退出可行性预检",
        "emergency_halt": "暂停本页演示入场检查",
        "resume_trading": "恢复本页演示检查",
        "halt_desc": "仅改变本页会话的风险检查状态，不会暂停或恢复测试网、实盘及其他进程。",
        # Settings
        "settings_title": "系统运行与风控设置",
        "settings_subtitle": "在此配置交易模式、本金上限、风控熔断阈值与网络代理，防止未经授权的资金敞口。",
        "set_mode_group": "运行模式与交易门禁",
        "set_mode_label": "交易模式选择",
        "set_mode_paper": "Paper 模拟盘 (默认，零资金风险)",
        "set_mode_testnet": "Spot Testnet 测试网 (隔离虚拟资金)",
        "set_mode_live": "Live 现货实盘 (严格三重门禁锁定)",
        "set_gates_header": "实盘准入检查清单 (Phase 5 门禁)",
        "gate_1": "环境变量 TRADING_MODE = live",
        "gate_2": "环境变量 ENABLE_LIVE_TRADING = true",
        "gate_3": "CLI 显式携带 --confirm-live 参数",
        "gate_4": "Preflight 检查通过 (余额、最小下单额、交易对状态)",
        "set_capital_group": "资金预算与风控参数",
        "set_capital_cap": "实盘初始本金上限 (USDT)",
        "set_capital_cap_help": "按照最多投入 50 元人民币折算，系统强制限定上限不得超过 10.00 USDT",
        "set_daily_loss": "单日亏损熔断比例 (占初始净值)",
        "set_daily_entries": "每个自然日最多新开仓次数",
        "set_drawdown": "出场可行性假设跌幅 (Exit Feasibility)",
        "set_drawdown_help": "入场前模拟币价下跌后能否满足 MIN_NOTIONAL 最小卖出额",
        "set_api_group": "API 凭据与安全状态",
        "set_proxy_group": "网络连接与代理自检",
        "set_proxy_url": "HTTP / HTTPS 代理地址",
        "set_proxy_btn": "测试币安接口连通性 (检验 451 限制)",
        "set_save_btn": "保存并更新当前设置",
        "set_save_ok": "已更新本网页会话中的演示风控参数；未修改 .env 或后台交易进程。",
        # Docs
        "docs_title": "JevQuant 核心设计哲学与安全规则",
        "doc_p1": "1. 50 元极小本金的特殊限制：币安 BTC/USDT 存在最小下单金额（通常约 5 USDT），50 元人民币本金无法进行网格或分批补仓，每一笔交易必须全额核算。",
        "doc_p2": "2. 为什么需要退出可行性预检（Exit Feasibility）：买入必须留出逆向跌幅空间。若跌 3% 后剩余持仓折合不足 5 USDT 将无法通过交易所规则卖出止损。遇到此类风险，系统会在买入前直接阻断（REJECT_EXIT_MIN_NOTIONAL）。",
        "doc_p3": "3. 权限彻底分离：TypeSafe Jev AI 仅输出 BUY/SELL/HOLD 建议，无权接触 API 密钥、无权决定下单数量、无权修改风控阈值。",
    },
    "en": {
        "title": "JEV QUANT DECISION ENGINE",
        "subtitle": "TYPESAFE JEV + BINANCE SPOT · ZERO LOOKAHEAD GUARANTEE · INDEPENDENT RISK LAYER",
        "nav_monitor": "Reflex Monitor",
        "nav_ledger": "Paper Ledger",
        "nav_settings": "Settings & Risk",
        "nav_docs": "Architecture & Invariants",
        "demo_banner": "[DEMO MODE] This dashboard uses synthetic candles and sample data, not live prices or measured model performance. Pause affects this browser session only.",
        "beginner_guide_title": "New here? What the numbers and buttons mean",
        "beginner_guide_body": "This is a demonstration with no exchange orders. BUY / SELL / HOLD are advisory actions, not actual trades. A dash (—) means no measured confidence was provided; the page never fabricates a value. Paper balances live only in this browser session. These controls cannot pause another process. Read Architecture & Invariants before considering Testnet.",
        "mode_preview_notice": "Execution mode options are illustrative only: Save does not switch to Testnet/Live or modify .env.",
        "kpi_decisions": "Total Jev Decisions",
        "kpi_avg_latency": "Avg Latency",
        "kpi_p95_latency": "P95 Latency",
        "kpi_sample_rate": "Sampling Period",
        "kpi_sample_rate_val": "1 Hour / Closed Candle",
        "sec_snapshots": "Synthetic Market Snapshots (Sample 1H Candles)",
        "sec_decision_out": "AI & Baseline Decision Output",
        "sec_causal_chain": "Latest Causal Chain",
        "sec_action_log": "Sample Action Log (Not Live)",
        "radar_title": "Demo Decision Ring",
        "radar_sub": "Structured Choice Engine",
        "card_price": "Last Price",
        "card_indicators": "Indicators",
        "decision_reason": "Decision Reason",
        "signal_id": "Deterministic Signal Fingerprint",
        "baseline_title": "SMA Baseline (Comparison Strategy)",
        "baseline_sub": "Zero API Cost Baseline",
        "portfolio_title": "Paper Portfolio Balance",
        "portfolio_equity": "Estimated Equity",
        "portfolio_free": "Available USDT",
        "portfolio_btc": "BTC Position",
        "portfolio_budget": "Initial Budget Cap",
        "heartbeat_title": "Decision Heartbeat",
        "heartbeat_meta": "Demo mode: no continuous runtime statistics",
        "risk_status": "Risk Decision",
        "exit_feasibility": "Exit Feasibility Pre-check",
        "emergency_halt": "Pause Local Demo Entry Checks",
        "resume_trading": "Resume Local Demo Checks",
        "halt_desc": "Only affects this browser session; does not pause or resume other Testnet/Live processes.",
        # Settings
        "settings_title": "System Configuration & Risk Controls",
        "settings_subtitle": "Configure execution modes, capital ceiling, circuit breakers, and network proxy to prevent unauthorized exposure.",
        "set_mode_group": "Execution Mode & Safety Gates",
        "set_mode_label": "Execution Mode",
        "set_mode_paper": "Paper Trading (Default, zero capital risk)",
        "set_mode_testnet": "Spot Testnet (Isolated virtual funds)",
        "set_mode_live": "Live Spot (Strict multi-gate locked)",
        "set_gates_header": "Live Trading Preflight Checklist (Phase 5 Gates)",
        "gate_1": "Env TRADING_MODE = live",
        "gate_2": "Env ENABLE_LIVE_TRADING = true",
        "gate_3": "CLI explicit flag --confirm-live",
        "gate_4": "Preflight passed (balance, min_notional, trading status)",
        "set_capital_group": "Capital Budget & Risk Parameters",
        "set_capital_cap": "Live Initial Capital Cap (USDT)",
        "set_capital_cap_help": "Calculated from max 50 CNY budget. Capped at 10.00 USDT maximum by hardcoded safety invariant.",
        "set_daily_loss": "Daily Loss Circuit Breaker (% of Initial Equity)",
        "set_daily_entries": "Max New Entries Per UTC Day",
        "set_drawdown": "Exit Feasibility Drawdown Buffer",
        "set_drawdown_help": "Simulates if adverse price move still allows legal exit above MIN_NOTIONAL.",
        "set_api_group": "API Credentials & Secret Status",
        "set_proxy_group": "Network & Proxy Verification",
        "set_proxy_url": "HTTP / HTTPS Proxy URL",
        "set_proxy_btn": "Test Binance API Connectivity (Check 451)",
        "set_save_btn": "Save & Apply Settings",
        "set_save_ok": "Updated demo risk values in this browser session only; .env and background processes are unchanged.",
        # Docs
        "docs_title": "JevQuant Core Philosophy & Non-negotiables",
        "doc_p1": "1. 50 CNY Micro-budget Constraints: Binance BTC/USDT has a dynamic minimum notional (~5 USDT). A 50 CNY budget cannot support grid or martingale layering.",
        "doc_p2": "2. Why Exit Feasibility Pre-check is Critical: Entering a trade is prohibited if a 3% drop leaves the position under 5 USDT, which would trap the position from legal exit.",
        "doc_p3": "3. Separation of Powers: TypeSafe Jev AI provides advisory direction (BUY/SELL/HOLD) only. It has zero order execution permissions.",
    }
}

t = I18N[st.session_state.lang]

# Styling CSS (Dark Industrial Terminal without emoji)
st.markdown("""
<style>
    @keyframes radarPulse {
        0% {
            box-shadow: 0 0 0 0 rgba(56, 189, 248, 0.4), inset 0 0 15px rgba(56, 189, 248, 0.1);
            border-color: rgba(56, 189, 248, 0.6);
        }
        50% {
            box-shadow: 0 0 35px 8px rgba(56, 189, 248, 0.15), inset 0 0 25px rgba(56, 189, 248, 0.2);
            border-color: rgba(56, 189, 248, 0.9);
        }
        100% {
            box-shadow: 0 0 0 0 rgba(56, 189, 248, 0.4), inset 0 0 15px rgba(56, 189, 248, 0.1);
            border-color: rgba(56, 189, 248, 0.6);
        }
    }

    @keyframes liveDotGlow {
        0%, 100% { opacity: 1; filter: drop-shadow(0 0 6px #eab308); }
        50% { opacity: 0.3; filter: drop-shadow(0 0 1px #eab308); }
    }

    .stApp {
        background-color: #0b0d13;
        color: #e2e8f0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    }

    /* Top Navigation Header */
    .app-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 1px solid #1a2336;
        padding-bottom: 14px;
        margin-bottom: 16px;
    }
    .brand-title {
        font-size: 1.35rem;
        font-weight: 800;
        letter-spacing: 0.14em;
        color: #f8fafc;
    }
    .brand-subtitle {
        font-size: 0.68rem;
        color: #64748b;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        margin-top: 3px;
    }

    /* Navigation Bar */
    .nav-bar-container {
        display: flex;
        gap: 8px;
        margin-bottom: 20px;
        border-bottom: 1px solid #161e2e;
        padding-bottom: 10px;
    }

    /* KPI Row */
    .kpi-row {
        display: flex;
        justify-content: space-around;
        align-items: baseline;
        background: #10141f;
        border: 1px solid #1c2538;
        border-radius: 8px;
        padding: 16px 24px;
        margin-bottom: 22px;
    }
    .kpi-block {
        text-align: center;
    }
    .kpi-num {
        font-size: 2.2rem;
        font-weight: 800;
        color: #ffffff;
        font-feature-settings: "tnum";
    }
    .kpi-label {
        font-size: 0.68rem;
        letter-spacing: 0.1em;
        color: #64748b;
        text-transform: uppercase;
        margin-top: -2px;
    }

    /* Market Cards */
    .market-card {
        background: #10141f;
        border: 1px solid #1a2336;
        border-radius: 6px;
        padding: 14px 18px;
        margin-bottom: 12px;
        transition: all 0.25s ease;
    }
    .market-card:hover {
        border-color: #2b3954;
        box-shadow: 0 4px 18px rgba(0, 0, 0, 0.4);
    }
    .market-card-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .market-symbol {
        font-size: 0.9rem;
        font-weight: 700;
        color: #94a3b8;
    }
    .market-price {
        font-size: 1.15rem;
        font-weight: 700;
        color: #f1f5f9;
        font-feature-settings: "tnum";
    }
    .market-action-row {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        margin-top: 8px;
    }
    .action-badge-hold {
        font-size: 1.25rem;
        font-weight: 800;
        color: #cbd5e1;
    }
    .action-badge-buy {
        font-size: 1.25rem;
        font-weight: 800;
        color: #22c55e;
    }
    .action-badge-sell {
        font-size: 1.25rem;
        font-weight: 800;
        color: #ef4444;
    }
    .action-percent {
        font-size: 1.05rem;
        color: #94a3b8;
        margin-left: 6px;
    }
    .market-latency {
        font-size: 0.75rem;
        color: #64748b;
    }

    /* Center Radar Circle */
    .radar-container {
        position: relative;
        width: 180px;
        height: 180px;
        margin: auto;
        display: flex;
        justify-content: center;
        align-items: center;
    }
    .radar-circle {
        position: relative;
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        width: 160px;
        height: 160px;
        border-radius: 50%;
        border: 2px solid #38bdf8;
        background: radial-gradient(circle, #151d2c 0%, #0c1017 100%);
        animation: radarPulse 2.6s infinite ease-in-out;
    }
    .radar-title {
        font-size: 0.62rem;
        letter-spacing: 0.16em;
        color: #38bdf8;
        text-transform: uppercase;
    }
    .radar-name {
        font-size: 1.85rem;
        font-weight: 900;
        letter-spacing: 0.12em;
        color: #ffffff;
        margin: 2px 0;
    }
    .radar-ms {
        font-size: 0.95rem;
        font-weight: 700;
        color: #38bdf8;
    }

    /* Log Panel */
    .log-panel {
        background: #0d111a;
        border: 1px solid #182133;
        border-radius: 6px;
        padding: 14px;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        font-size: 0.78rem;
        color: #94a3b8;
        line-height: 1.65;
    }

    /* Settings Sections */
    .settings-card {
        background: #10141f;
        border: 1px solid #1d263b;
        border-radius: 8px;
        padding: 20px;
        margin-bottom: 20px;
    }
    .settings-card-title {
        font-size: 0.95rem;
        font-weight: 700;
        color: #f1f5f9;
        margin-bottom: 12px;
        letter-spacing: 0.05em;
    }
</style>
""", unsafe_allow_html=True)

# Top Bar: Brand + Language Toggle
header_col1, header_col2 = st.columns([3, 1])
with header_col1:
    st.markdown(f"""
    <div class="brand-title">{t['title']}</div>
    <div class="brand-subtitle">{t['subtitle']}</div>
    """, unsafe_allow_html=True)

with header_col2:
    btn_zh_type = "primary" if st.session_state.lang == "zh" else "secondary"
    btn_en_type = "primary" if st.session_state.lang == "en" else "secondary"
    l_col1, l_col2 = st.columns(2)
    with l_col1:
        if st.button("中文", type=btn_zh_type, use_container_width=True):
            st.session_state.lang = "zh"
            st.rerun()
    with l_col2:
        if st.button("English", type=btn_en_type, use_container_width=True):
            st.session_state.lang = "en"
            st.rerun()

st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)

# Top Navigation Tabs
nav_items = [
    ("monitor", t["nav_monitor"]),
    ("ledger", t["nav_ledger"]),
    ("settings", t["nav_settings"]),
    ("docs", t["nav_docs"]),
]

cols = st.columns(len(nav_items))
for idx, (tab_key, tab_label) in enumerate(nav_items):
    with cols[idx]:
        is_active = st.session_state.nav_tab == tab_key
        btn_type = "primary" if is_active else "secondary"
        if st.button(tab_label, key=f"nav_{tab_key}", type=btn_type, use_container_width=True):
            st.session_state.nav_tab = tab_key
            st.rerun()

st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

# Demo Warning Notice
st.warning(t["demo_banner"])
with st.expander(t["beginner_guide_title"], expanded=False):
    st.write(t["beginner_guide_body"])

# Helper: Market Data Generator
def get_or_create_market_data():
    now_ts = int(time.time() // 3600 * 3600)
    data = []
    base_btc = 68450.0
    for i in range(65, 0, -1):
        ts = now_ts - i * 3600
        p = base_btc + (i % 7) * 120.0 - (i % 5) * 80.0
        data.append({
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "candle_close_utc": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "open": Decimal(str(p - 30)),
            "high": Decimal(str(p + 95)),
            "low": Decimal(str(p - 60)),
            "close": Decimal(str(p)),
            "volume": Decimal("185.4"),
        })
    return data

candles = get_or_create_market_data()
indicators = compute_indicators(candles)
latest_candle = candles[-1]
current_price = latest_candle["close"]

# Engine & Strategy Evaluation
jev_engine = JevEngine(api_key=cfg.typesafe_api_key)
ai_strategy = JevAIStrategy(jev_engine=jev_engine)
sma_strategy = SmaBaselineStrategy()

ai_sig = ai_strategy.evaluate(
    symbol=cfg.symbol,
    candle_close_utc=latest_candle["candle_close_utc"],
    indicators=indicators,
    current_close=current_price,
    has_position=paper.has_position,
)

sma_sig = sma_strategy.evaluate(
    symbol=cfg.symbol,
    candle_close_utc=latest_candle["candle_close_utc"],
    indicators=indicators,
    has_position=paper.has_position,
)

market_rules = {
    "symbol": "BTC/USDT",
    "status": "TRADING",
    "min_notional": Decimal("5.00000000"),
    "step_size": Decimal("0.00001"),
    "min_qty": Decimal("0.00001"),
    "min_price": Decimal("0.01"),
    "max_price": Decimal("1000000.00"),
    "tick_size": Decimal("0.01"),
}

risk_decision = risk.evaluate_entry(
    signal_action=ai_sig.action,
    current_price=current_price,
    market_rules=market_rules,
    available_usdt=paper.usdt_balance,
    has_position=paper.has_position,
    today_realized_loss_usdt=paper.today_realized_loss_usdt,
)
confidence_text = format_confidence(ai_sig.metadata.get("confidence"))
exit_status = (
    "DEMO PASS" if risk_decision.approved
    else "REJECT_EXIT_MIN_NOTIONAL" if risk_decision.reason_code == "REJECT_EXIT_MIN_NOTIONAL"
    else "—"
)
exit_color = (
    "#22c55e" if risk_decision.approved
    else "#ef4444" if risk_decision.reason_code == "REJECT_EXIT_MIN_NOTIONAL"
    else "#94a3b8"
)


# ---------------- PAGE 1: MONITOR (决策监控台) ----------------
if st.session_state.nav_tab == "monitor":
    # KPI Row
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.markdown(f"""
        <div class="kpi-block">
            <div class="kpi-num">—</div>
            <div class="kpi-label">{t['kpi_decisions']}</div>
        </div>
        """, unsafe_allow_html=True)
    with k2:
        lat = format_latency_ms(ai_sig.metadata.get("latency_ms"))
        st.markdown(f"""
        <div class="kpi-block">
            <div class="kpi-num">{lat}</div>
            <div class="kpi-label">{t['kpi_avg_latency']}</div>
        </div>
        """, unsafe_allow_html=True)
    with k3:
        st.markdown(f"""
        <div class="kpi-block">
            <div class="kpi-num">—</div>
            <div class="kpi-label">{t['kpi_p95_latency']}</div>
        </div>
        """, unsafe_allow_html=True)
    with k4:
        st.markdown(f"""
        <div class="kpi-block">
            <div class="kpi-num" style="font-size:1.6rem; margin-top:6px;">{t['kpi_sample_rate_val']}</div>
            <div class="kpi-label">{t['kpi_sample_rate']}</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    # 3-Column Arena
    c_left, c_mid, c_right = st.columns([1.1, 1.2, 1.1])

    with c_left:
        st.markdown(f"<div style='font-size:0.75rem; letter-spacing:0.12em; color:#64748b; margin-bottom:10px;'>{t['sec_snapshots']}</div>", unsafe_allow_html=True)
        # BTC Card
        st.markdown(f"""
        <div class="market-card">
            <div class="market-card-header">
                <span class="market-symbol">BTC/USDT</span>
                <span class="market-price">${current_price:,.2f}</span>
            </div>
            <div class="market-action-row">
                <div>
                    <span class="action-badge-hold">{ai_sig.action}</span>
                    <span class="action-percent">{confidence_text}</span>
                </div>
                <span class="market-latency">{lat}</span>
            </div>
            <div style="font-size: 0.72rem; color: #64748b; margin-top: 8px;">
                SMA20: {indicators['sma20']} · SMA60: {indicators['sma60']} · RSI: {indicators['rsi14']}
            </div>
        </div>
        """, unsafe_allow_html=True)

        # ETH Simulated Card
        st.markdown("""
        <div class="market-card">
            <div class="market-card-header">
                <span class="market-symbol">ETH/USDT [SAMPLE]</span>
                <span class="market-price">$2,638.84</span>
            </div>
            <div class="market-action-row">
                <div>
                    <span class="action-badge-hold">HOLD</span>
                    <span class="action-percent">54%</span>
                </div>
                <span class="market-latency">218 ms</span>
            </div>
            <div style="font-size: 0.72rem; color: #64748b; margin-top: 8px;">
                SMA20: 2,610.2 · SMA60: 2,580.4 · RSI: 52.3
            </div>
        </div>
        """, unsafe_allow_html=True)

        # SOL Simulated Card
        st.markdown("""
        <div class="market-card">
            <div class="market-card-header">
                <span class="market-symbol">SOL/USDT [SAMPLE]</span>
                <span class="market-price">$111.85</span>
            </div>
            <div class="market-action-row">
                <div>
                    <span class="action-badge-hold">HOLD</span>
                    <span class="action-percent">29%</span>
                </div>
                <span class="market-latency">245 ms</span>
            </div>
            <div style="font-size: 0.72rem; color: #64748b; margin-top: 8px;">
                SMA20: 109.8 · SMA60: 108.2 · RSI: 48.7
            </div>
        </div>
        """, unsafe_allow_html=True)

    with c_mid:
        st.markdown("<div style='height: 40px;'></div>", unsafe_allow_html=True)
        st.markdown(f"""
        <div class="radar-container">
            <div class="radar-circle">
                <div class="radar-title">{t['radar_title']}</div>
                <div class="radar-name">JEV</div>
                <div style="font-size:0.62rem; color:#94a3b8; letter-spacing:0.1em;">{t['radar_sub']}</div>
                <div class="radar-ms">{lat}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    with c_right:
        st.markdown(f"<div style='font-size:0.75rem; letter-spacing:0.12em; color:#64748b; margin-bottom:10px;'>{t['sec_decision_out']}</div>", unsafe_allow_html=True)

        # Jev Output Card
        st.markdown(f"""
        <div class="market-card" style="border-left: 3px solid #38bdf8;">
            <div class="market-card-header">
                <span class="market-symbol">BTC/USDT</span>
                <span style="font-size: 0.75rem; color: #64748b;">{latest_candle['candle_close_utc'][-13:-4]}</span>
            </div>
            <div class="market-action-row" style="margin-top: 14px;">
                <span class="action-badge-hold" style="font-size: 1.8rem;">{ai_sig.action}</span>
                <span class="action-percent" style="font-size: 1.4rem; color: #38bdf8;">{confidence_text}</span>
            </div>
            <div style="margin-top: 10px; font-size: 0.75rem; color: #94a3b8;">
                <b>{t['decision_reason']}:</b> {ai_sig.reason_code}<br>
                <b>{t['signal_id']}:</b> <code>{ai_sig.signal_id}</code>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Baseline Strategy Card
        st.markdown(f"""
        <div class="market-card" style="border-left: 3px solid #64748b; opacity: 0.88;">
            <div class="market-card-header">
                <span class="market-symbol">{t['baseline_title']}</span>
                <span style="font-size: 0.75rem; color: #64748b;">{t['baseline_sub']}</span>
            </div>
            <div class="market-action-row" style="margin-top: 10px;">
                <span class="action-badge-hold" style="font-size: 1.4rem;">{sma_sig.action}</span>
                <span style="font-size: 0.8rem; color: #94a3b8;">SMA20 vs SMA60</span>
            </div>
            <div style="margin-top: 8px; font-size: 0.75rem; color: #64748b;">
                {sma_sig.reason_code}
            </div>
        </div>
        """, unsafe_allow_html=True)

    # Bottom Panels: Causal Chain & Logs
    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)
    b1, b2 = st.columns([1, 2])
    with b1:
        st.markdown(f"<div style='font-size:0.75rem; letter-spacing:0.12em; color:#64748b; margin-bottom:8px;'>{t['sec_causal_chain']}</div>", unsafe_allow_html=True)
        st.markdown(f"""
        <div class="log-panel">
            <div><b>BTC/USDT</b> 1h candle confirmed</div>
            <div>Jev Output: <span style="color:#e2e8f0">{ai_sig.action} ({confidence_text})</span></div>
            <div>Latency: <span style="color:#38bdf8">{lat}</span></div>
            <div>{t['risk_status']}: <span style="color:{'#22c55e' if risk_decision.approved else '#f59e0b'}">{risk_decision.reason_code}</span></div>
            <div>{t['exit_feasibility']}: <span style="color:{exit_color}">{exit_status}</span></div>
        </div>
        """, unsafe_allow_html=True)

    with b2:
        st.markdown(f"<div style='font-size:0.75rem; letter-spacing:0.12em; color:#64748b; margin-bottom:8px;'>{t['sec_action_log']}</div>", unsafe_allow_html=True)
        st.markdown("""
        <div class="log-panel">
            <div>[SAMPLE] 12:00:00 UTC - BTC 1h closed - Indicators calculated - Jev queried</div>
            <div>[SAMPLE] 12:00:00 UTC - Signal generated - Hash: sig_deterministic_confirmed</div>
            <div>[SAMPLE] 12:00:00 UTC - Risk evaluation - Position: FLAT - Budget: 7.00 USDT - PASS</div>
            <div>[SAMPLE] 11:00:00 UTC - BTC 1h closed - Action: HOLD (Confidence 65%) - No Order</div>
        </div>
        """, unsafe_allow_html=True)


# ---------------- PAGE 2: LEDGER (模拟账本与持仓) ----------------
elif st.session_state.nav_tab == "ledger":
    st.markdown(f"### {t['portfolio_title']}")
    current_equity = paper.get_equity(current_price)

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric(t["portfolio_equity"], f"${current_equity:,.2f} USDT")
    with m2:
        st.metric(t["portfolio_free"], f"${paper.usdt_balance:,.2f} USDT")
    with m3:
        st.metric(t["portfolio_btc"], f"{paper.btc_balance:.6f} BTC")
    with m4:
        st.metric(t["portfolio_budget"], f"${paper.initial_usdt:.2f} USDT")

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)
    st.markdown("#### 模拟交易明细 (Trade Fills)")

    if paper.trades:
        trade_rows = []
        for tr in paper.trades:
            trade_rows.append({
                "Client Order ID": tr.client_order_id,
                "Time (UTC)": tr.timestamp_utc,
                "Side": tr.side,
                "Price": f"${tr.price:,.2f}",
                "Quantity": f"{tr.qty:.6f}",
                "Notional": f"${tr.notional:.2f}",
                "Fee": f"${tr.fee_usdt:.4f} USDT",
            })
        st.dataframe(pd.DataFrame(trade_rows), use_container_width=True)
    else:
        st.info("当前暂无已成交交易记录。当前演示页面不会自动执行买卖，因此没有成交记录；后续接入模拟执行流程后才会显示交易明细。")

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)
    st.markdown("#### 退出可行性预检公式 (Exit Feasibility Math)")
    st.markdown("""
    ```text
    可用买入预算 = min(账户USDT, 初始上限)
    买入扣费后 BTC 数量 = (预算 / 当前价格) * (1 - 买入手续费 0.1%)
    按交易所 LOT_SIZE stepSize 向下取整
    假设发生不利逆向跌幅 (-3% 跌幅 - 0.05% 滑点)
    预计卖出名义额 = 可卖数量 * (当前价 * (1 - 3% - 0.05%)) * (1 - 卖出手续费 0.1%)
    校验条件: 预计卖出名义额 >= MIN_NOTIONAL (5 USDT)
    结果: 若小于 5 USDT 则【坚决拒绝开仓】(REJECT_EXIT_MIN_NOTIONAL)
    ```
    """)


# ---------------- PAGE 3: SETTINGS (系统与风控设置) ----------------
elif st.session_state.nav_tab == "settings":
    st.markdown(f"### {t['settings_title']}")
    st.caption(t["settings_subtitle"])

    # 1. Mode & Gates
    st.markdown(f"#### 1. {t['set_mode_group']}")
    with st.container():
        mode_options = ["paper", "testnet", "live"]
        mode_labels = [t["set_mode_paper"], t["set_mode_testnet"], t["set_mode_live"]]
        current_idx = mode_options.index(cfg.trading_mode) if cfg.trading_mode in mode_options else 0
        selected_mode = st.radio(t["set_mode_label"], mode_options, index=current_idx, format_func=lambda x: mode_labels[mode_options.index(x)])
        st.caption(t["mode_preview_notice"])

        # Gates checklist
        st.markdown(f"**{t['set_gates_header']}**")
        g1 = cfg.trading_mode == "live"
        g2 = cfg.enable_live_trading
        g3 = False  # CLI parameter only
        st.markdown(f"- [{'x' if g1 else ' '}] {t['gate_1']}")
        st.markdown(f"- [{'x' if g2 else ' '}] {t['gate_2']}")
        st.markdown(f"- [{'x' if g3 else ' '}] {t['gate_3']}")
        st.markdown(f"- [{'x' if g1 and g2 and g3 else ' '}] {t['gate_4']}")

    st.markdown("---")

    # 2. Capital & Risk Limits
    st.markdown(f"#### 2. {t['set_capital_group']}")
    c_col1, c_col2 = st.columns(2)
    with c_col1:
        new_capital = st.number_input(
            t["set_capital_cap"],
            min_value=5.00,
            max_value=10.00,
            value=float(risk.initial_capital_usdt),
            step=0.50,
            help=t["set_capital_cap_help"],
        )
        new_daily_loss = st.slider(
            t["set_daily_loss"],
            min_value=0.01,
            max_value=0.05,
            value=float(risk.daily_loss_halt_pct),
            step=0.005,
            format="%.3f",
        )
    with c_col2:
        new_entries = st.number_input(
            t["set_daily_entries"],
            min_value=1,
            max_value=3,
            value=int(risk.max_daily_entries),
            step=1,
        )
        new_drawdown = st.slider(
            t["set_drawdown"],
            min_value=0.01,
            max_value=0.08,
            value=float(risk.reference_drawdown_pct),
            step=0.005,
            format="%.3f",
            help=t["set_drawdown_help"],
        )

    st.markdown("---")

    # 3. API Credentials & Status (Masked)
    st.markdown(f"#### 3. {t['set_api_group']}")
    api1, api2, api3 = st.columns(3)
    with api1:
        has_ts = bool(cfg.typesafe_api_key)
        st.markdown(f"**TypeSafe Jev API Key**<br>`{'配置有效 (已掩码保护)' if has_ts else '未配置 (使用本地降级模式)'}`", unsafe_allow_html=True)
    with api2:
        has_testnet = bool(cfg.binance_testnet_api_key and cfg.binance_testnet_api_secret)
        st.markdown(f"**Binance Testnet Key**<br>`{'配置有效 (已掩码保护)' if has_testnet else '未配置'}`", unsafe_allow_html=True)
    with api3:
        has_live = bool(cfg.binance_api_key and cfg.binance_api_secret)
        st.markdown(f"**Binance Live Key**<br>`{'配置有效 (已掩码保护)' if has_live else '未配置 (安全锁定)'}`", unsafe_allow_html=True)

    st.markdown("---")

    # 4. Proxy & Connectivity Check
    st.markdown(f"#### 4. {t['set_proxy_group']}")
    proxy_val = st.text_input(t["set_proxy_url"], value=os.getenv("HTTPS_PROXY", "http://127.0.0.1:7890"))
    if st.button(t["set_proxy_btn"], use_container_width=True):
        try:
            adapter = BinancePublicAdapter(proxy=proxy_val)
            rules = adapter.fetch_market_rules("BTC/USDT")
            st.session_state.proxy_check_result = f"连通成功！成功解析规则：MIN_NOTIONAL={rules['min_notional']} USDT，状态={rules['status']}"
        except Exception as err:
            st.session_state.proxy_check_result = f"连通异常：{type(err).__name__}: {str(err)}"

    if st.session_state.proxy_check_result:
        st.code(st.session_state.proxy_check_result)

    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)

    # Save button
    if st.button(t["set_save_btn"], type="primary", use_container_width=True):
        risk.initial_capital_usdt = Decimal(str(new_capital))
        risk.daily_loss_halt_pct = Decimal(str(new_daily_loss))
        risk.max_daily_entries = int(new_entries)
        risk.reference_drawdown_pct = Decimal(str(new_drawdown))
        st.success(t["set_save_ok"])


# ---------------- PAGE 4: DOCS (架构与安全规则) ----------------
elif st.session_state.nav_tab == "docs":
    st.markdown(f"### {t['docs_title']}")
    st.markdown(f"""
    > {t['doc_p1']}

    > {t['doc_p2']}

    > {t['doc_p3']}
    """)
    st.markdown("---")
    st.markdown("#### 核心时序图 (Core Sequence)")
    st.code("""
行情获取(仅已收盘K线) -> 指标计算(SMA/RSI/ATR) -> Jev结构化建议(BUY/SELL/HOLD)
     |
     v
独立风控校验 (出场可行性、50元上限、日亏损3%熔断、单仓位限制)
     |
     +--> 拒绝: 记录原因并保持 FLAT / HOLD
     |
     +--> 批准: 经由唯一 OrderExecutor 幂等执行 -> 记录 SQLite 审计
    """, language="text")

# Emergency Halt Controller Bar (Always visible at bottom)
st.markdown("---")
ctrl_c1, ctrl_c2 = st.columns([2, 1])
with ctrl_c1:
    st.markdown(f"**{t['emergency_halt']}**")
    st.caption(t["halt_desc"])

with ctrl_c2:
    if risk.is_halted:
        if st.button(t["resume_trading"], use_container_width=True):
            risk.is_halted = False
            risk.halt_reason = None
            st.rerun()
    else:
        if st.button(t["emergency_halt"], type="primary", use_container_width=True):
            risk.is_halted = True
            risk.halt_reason = "MANUAL_UI_EMERGENCY_HALT"
            st.rerun()
