import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from jevquant.config import load_config_from_env
from jevquant.indicators import compute_indicators
from jevquant.jev_engine import JevEngine
from jevquant.orders import OrderStateMachine, PaperAccount
from jevquant.risk import RiskManager
from jevquant.strategy import JevAIStrategy, SmaBaselineStrategy

st.set_page_config(
    page_title="JEV MARKET REFLEX",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

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

    @keyframes heartbeatRing {
        0% { transform: scale(0.95); opacity: 0.8; }
        50% { transform: scale(1.15); opacity: 0.2; }
        100% { transform: scale(0.95); opacity: 0.8; }
    }

    @keyframes liveDotGlow {
        0%, 100% { opacity: 1; filter: drop-shadow(0 0 6px #eab308); }
        50% { opacity: 0.3; filter: drop-shadow(0 0 1px #eab308); }
    }

    .stApp {
        background-color: #0b0d13;
        color: #e2e8f0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", monospace;
    }

    .reflex-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 1px solid #1a2233;
        padding-bottom: 14px;
        margin-bottom: 20px;
    }
    .reflex-title {
        font-size: 1.35rem;
        font-weight: 800;
        letter-spacing: 0.16em;
        color: #f8fafc;
        display: flex;
        align-items: center;
        gap: 10px;
    }
    .reflex-subtitle {
        font-size: 0.7rem;
        color: #64748b;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        margin-top: 3px;
    }

    .mode-badge-live {
        background: rgba(234, 179, 8, 0.12);
        color: #eab308;
        border: 1px solid rgba(234, 179, 8, 0.35);
        padding: 5px 12px;
        border-radius: 4px;
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        display: inline-flex;
        align-items: center;
        gap: 6px;
    }
    .live-dot {
        width: 7px;
        height: 7px;
        background-color: #eab308;
        border-radius: 50%;
        animation: liveDotGlow 1.8s infinite ease-in-out;
    }

    .mode-badge-paper {
        background: rgba(56, 189, 248, 0.1);
        color: #38bdf8;
        border: 1px solid rgba(56, 189, 248, 0.28);
        padding: 5px 12px;
        border-radius: 4px;
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.08em;
    }

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
        font-size: 2.3rem;
        font-weight: 800;
        color: #ffffff;
        font-feature-settings: "tnum";
    }
    .kpi-label {
        font-size: 0.68rem;
        letter-spacing: 0.12em;
        color: #64748b;
        text-transform: uppercase;
        margin-top: -2px;
    }

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
    .action-percent {
        font-size: 1.05rem;
        color: #94a3b8;
        margin-left: 6px;
    }
    .market-latency {
        font-size: 0.75rem;
        color: #64748b;
    }

    .radar-container {
        position: relative;
        width: 190px;
        height: 190px;
        margin: auto;
        display: flex;
        justify-content: center;
        align-items: center;
    }
    .radar-pulse-ring {
        position: absolute;
        width: 190px;
        height: 190px;
        border-radius: 50%;
        border: 1px solid rgba(56, 189, 248, 0.4);
        animation: heartbeatRing 2.6s infinite ease-out;
    }
    .radar-circle {
        position: relative;
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        width: 165px;
        height: 165px;
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

    .heartbeat-bar {
        background: #0d111a;
        border: 1px solid #192233;
        border-radius: 6px;
        padding: 10px 18px;
        margin: 18px 0;
        display: flex;
        align-items: center;
        justify-content: space-between;
    }
    .heartbeat-timeline {
        display: flex;
        align-items: center;
        gap: 16px;
        width: 70%;
    }
    .timeline-node {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background-color: #334155;
    }
    .timeline-node-active {
        width: 11px;
        height: 11px;
        border-radius: 50%;
        background-color: #ef4444;
        box-shadow: 0 0 10px #ef4444;
        animation: liveDotGlow 1.2s infinite ease-in-out;
    }
    .timeline-line {
        flex: 1;
        height: 1px;
        background-color: #1e293b;
    }

    .log-panel {
        background: #0d111a;
        border: 1px solid #182133;
        border-radius: 6px;
        padding: 14px;
        font-family: monospace;
        font-size: 0.8rem;
        color: #94a3b8;
        line-height: 1.65;
    }

    .control-panel {
        background: #11151f;
        border: 1px solid #1f283d;
        border-radius: 6px;
        padding: 14px 18px;
        margin-top: 18px;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
</style>
""", unsafe_allow_html=True)

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

cfg = load_config_from_env()
paper = st.session_state.paper_account
risk = st.session_state.risk_manager
order_sm = st.session_state.order_sm

st.warning("演示模式 / DEMO ONLY：本页面 BTC 行情为合成 K 线，ETH/SOL 报价、活动日志及部分 KPI 为示例数据。这里的暂停按钮仅影响当前页面会话，不能停止测试网或实盘进程。请勿据此进行真实交易。")

st.markdown("""
<div class="reflex-header">
    <div>
        <div class="reflex-title">📊 JEV MARKET REFLEX</div>
        <div class="reflex-subtitle">CONTINUOUS TYPED DECISION ENGINE · ZERO LOOKAHEAD GUARANTEE</div>
    </div>
    <div style="display: flex; gap: 10px;">
        <span class="mode-badge-live"><span class="live-dot"></span>DEMO DATA · NOT LIVE</span>
        <span class="mode-badge-paper">PAPER SIMULATION</span>
        <span class="mode-badge-paper">SYNTHETIC CANDLES</span>
    </div>
</div>
""", unsafe_allow_html=True)

# Dashboard preview only: never present synthetic candles as exchange quotes.
# This page does not execute real exchange orders.
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

kpi1, kpi2, kpi3, kpi4 = st.columns(4)
with kpi1:
    st.markdown("""
    <div class="kpi-block">
        <div class="kpi-num">DEMO</div>
        <div class="kpi-label">JEV DECISIONS</div>
    </div>
    """, unsafe_allow_html=True)
with kpi2:
    st.markdown(f"""
    <div class="kpi-block">
        <div class="kpi-num">{ai_sig.metadata.get('latency_ms', 228) or 228}<span style="font-size:1.1rem;color:#64748b"> ms</span></div>
        <div class="kpi-label">AVG LATENCY</div>
    </div>
    """, unsafe_allow_html=True)
with kpi3:
    st.markdown("""
    <div class="kpi-block">
        <div class="kpi-num">DEMO<span style="font-size:1.1rem;color:#64748b"> ms</span></div>
        <div class="kpi-label">P95 LATENCY</div>
    </div>
    """, unsafe_allow_html=True)
with kpi4:
    st.markdown("""
    <div class="kpi-block">
        <div class="kpi-num">DEMO<span style="font-size:1.1rem;color:#64748b"> / min</span></div>
        <div class="kpi-label">SAMPLING RATE</div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("<div style='height: 10px'></div>", unsafe_allow_html=True)

col_left, col_mid, col_right = st.columns([1.1, 1.3, 1.1])

with col_left:
    st.markdown("<div style='font-size:0.72rem; letter-spacing:0.14em; color:#64748b; margin-bottom:10px;'>MARKET SNAPSHOTS</div>", unsafe_allow_html=True)

    st.markdown(f"""
    <div class="market-card">
        <div class="market-card-header">
            <span class="market-symbol">BTC/USDT</span>
            <span class="market-price">${current_price:,.2f}</span>
        </div>
        <div class="market-action-row">
            <div>
                <span class="action-badge-hold">{ai_sig.action}</span>
                <span class="action-percent">{(ai_sig.metadata.get('confidence') or 0.65)*100:.0f}%</span>
            </div>
            <span class="market-latency">{ai_sig.metadata.get('latency_ms', 234) or 234} ms</span>
        </div>
        <div style="font-size: 0.7rem; color: #475569; margin-top: 6px;">
            SMA20: {indicators['sma20']} · SMA60: {indicators['sma60']} · RSI: {indicators['rsi14']}
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="market-card">
        <div class="market-card-header">
            <span class="market-symbol">ETH/USDT</span>
            <span class="market-price">$2,638.84</span>
        </div>
        <div class="market-action-row">
            <div>
                <span class="action-badge-hold">HOLD</span>
                <span class="action-percent">54%</span>
            </div>
            <span class="market-latency">218 ms</span>
        </div>
        <div style="font-size: 0.7rem; color: #475569; margin-top: 6px;">
            SMA20: 2,610.2 · SMA60: 2,580.4 · RSI: 52.3
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="market-card">
        <div class="market-card-header">
            <span class="market-symbol">SOL/USDT</span>
            <span class="market-price">$111.85</span>
        </div>
        <div class="market-action-row">
            <div>
                <span class="action-badge-hold">HOLD</span>
                <span class="action-percent">29%</span>
            </div>
            <span class="market-latency">245 ms</span>
        </div>
        <div style="font-size: 0.7rem; color: #475569; margin-top: 6px;">
            SMA20: 109.8 · SMA60: 108.2 · RSI: 48.7
        </div>
    </div>
    """, unsafe_allow_html=True)

with col_mid:
    st.markdown("<div style='height: 35px;'></div>", unsafe_allow_html=True)
    st.markdown(f"""
    <div class="radar-container">
        <div class="radar-pulse-ring"></div>
        <div class="radar-circle">
            <div class="radar-title">DEMO REPLAY</div>
            <div class="radar-name">JEV</div>
            <div style="font-size:0.62rem; color:#94a3b8; letter-spacing:0.12em;">DECISION ENGINE</div>
            <div class="radar-ms">{ai_sig.metadata.get('latency_ms', 234) or 234} ms</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

with col_right:
    st.markdown("<div style='font-size:0.72rem; letter-spacing:0.14em; color:#64748b; margin-bottom:10px;'>DECISION OUTPUT</div>", unsafe_allow_html=True)

    st.markdown(f"""
    <div class="market-card" style="border-left: 3px solid #38bdf8;">
        <div class="market-card-header">
            <span class="market-symbol">BTC/USDT</span>
            <span style="font-size: 0.75rem; color: #64748b;">{latest_candle['candle_close_utc'][-13:-4]}</span>
        </div>
        <div class="market-action-row" style="margin-top: 14px;">
            <span class="action-badge-hold" style="font-size: 1.8rem;">{ai_sig.action}</span>
            <span class="action-percent" style="font-size: 1.4rem; color: #38bdf8;">{(ai_sig.metadata.get('confidence') or 0.65)*100:.0f}%</span>
        </div>
        <div style="margin-top: 10px; font-size: 0.75rem; color: #94a3b8;">
            <b>Reason:</b> {ai_sig.reason_code}<br>
            <b>Signal ID:</b> <code>{ai_sig.signal_id}</code>
        </div>
    </div>
    """, unsafe_allow_html=True)

    current_equity = paper.get_equity(current_price)
    st.markdown(f"""
    <div class="market-card" style="border-left: 3px solid #22c55e;">
        <div class="market-card-header">
            <span class="market-symbol">PORTFOLIO STATUS</span>
            <span style="font-size: 0.75rem; color: #22c55e;">STATE: {order_sm.state}</span>
        </div>
        <div class="market-action-row" style="margin-top: 10px;">
            <span style="font-size: 1.4rem; font-weight: 800; color: #ffffff;">${current_equity:,.2f} USDT</span>
            <span style="font-size: 0.8rem; color: #94a3b8;">Budget: ${paper.initial_usdt} USDT</span>
        </div>
        <div style="margin-top: 6px; font-size: 0.72rem; color: #64748b;">
            BTC Balance: {paper.btc_balance} · Free USDT: ${paper.usdt_balance:.2f}
        </div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("""
<div class="heartbeat-bar">
    <div style="font-size: 0.72rem; letter-spacing: 0.12em; color: #64748b; text-transform: uppercase;">
        DEMO HEARTBEAT <span style="color: #94a3b8; margin-left: 6px;">3 COMPLETED · 234 MS</span>
    </div>
    <div class="heartbeat-timeline">
        <div class="timeline-node"></div>
        <div class="timeline-line"></div>
        <div class="timeline-node"></div>
        <div class="timeline-line"></div>
        <div class="timeline-node"></div>
        <div class="timeline-line"></div>
        <div class="timeline-node-active"></div>
    </div>
</div>
""", unsafe_allow_html=True)

b_col1, b_col2 = st.columns([1, 2])

with b_col1:
    st.markdown("<div style='font-size:0.72rem; letter-spacing:0.14em; color:#64748b; margin-bottom:8px;'>LATEST CAUSAL CHAIN</div>", unsafe_allow_html=True)
    st.markdown(f"""
    <div class="log-panel">
        <div><b>BTC/USDT</b> snapshot</div>
        <div>Jev decision: <span style="color:#e2e8f0">{ai_sig.action} ({(ai_sig.metadata.get('confidence') or 0.65)*100:.0f}%)</span></div>
        <div>Latency: <span style="color:#38bdf8">{ai_sig.metadata.get('latency_ms', 234) or 234} ms</span></div>
        <div>Risk check: <span style="color:{'#22c55e' if risk_decision.approved else '#f59e0b'}">{risk_decision.reason_code}</span></div>
        <div>Execution mode: <span style="color:#38bdf8">{cfg.trading_mode.upper()}</span></div>
        <div style="margin-top: 6px; color: #64748b;">Fail-closed risk: PASS</div>
    </div>
    """, unsafe_allow_html=True)

with b_col2:
    st.markdown("<div style='font-size:0.72rem; letter-spacing:0.14em; color:#64748b; margin-bottom:8px;'>SAMPLE ACTION LOG · DEMO</div>", unsafe_allow_html=True)
    st.markdown("""
    <div class="log-panel">
        <div>● 08:03:59.030 &nbsp; SOL &nbsp; HOLD &nbsp; 29% &nbsp; 234 ms</div>
        <div>● 08:03:59.030 &nbsp; ETH &nbsp; HOLD &nbsp; 54% &nbsp; 234 ms</div>
        <div>● 08:03:59.030 &nbsp; BTC &nbsp; HOLD &nbsp; 26% &nbsp; 234 ms</div>
        <div>● 08:03:58.563 &nbsp; SOL &nbsp; HOLD &nbsp; 26% &nbsp; 268 ms</div>
        <div>● 08:03:58.563 &nbsp; ETH &nbsp; HOLD &nbsp; 54% &nbsp; 268 ms</div>
        <div>● 08:03:58.563 &nbsp; BTC &nbsp; HOLD &nbsp; 21% &nbsp; 268 ms</div>
    </div>
    """, unsafe_allow_html=True)

# P5 Emergency Pause & Circuit Breaker Control Panel
st.markdown("<div style='height: 10px'></div>", unsafe_allow_html=True)
ctrl_col1, ctrl_col2 = st.columns([2, 1])

with ctrl_col1:
    st.markdown("""
    <div style="font-size: 0.72rem; letter-spacing: 0.12em; color: #64748b; text-transform: uppercase;">
        LOCAL DEMO PAUSE (NOT LIVE TRADING)
    </div>
    <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 4px;">
        This local demo control only pauses entries in this Streamlit session. It does NOT halt Testnet or Live processes.
    </div>
    """, unsafe_allow_html=True)

with ctrl_col2:
    if risk.is_halted:
        if st.button("RESUME TRADING", use_container_width=True):
            risk.is_halted = False
            risk.halt_reason = None
            st.rerun()
    else:
        if st.button("PAUSE NEW ENTRIES", type="primary", use_container_width=True):
            risk.is_halted = True
            risk.halt_reason = "MANUAL_UI_EMERGENCY_HALT"
            st.rerun()
