# JevQuant

基于 **TypeSafe Jev + 币安现货 BTC/USDT** 的轻量级自动交易研究项目。用真实公开行情计算技术指标，由 Jev 生成结构化交易建议，经独立风控审批后由统一订单接口支持模拟盘（Paper）、测试网（Testnet）与受限小额实盘。

本金规划上限为 50 元人民币等值 USDT，不使用杠杆、不自动充值、不提现。第一目标是架构正确、可测试、可审计。

## 架构原则与安全红线

1. **AI 与交易权限彻底分离**：TypeSafe Jev 仅提供方向建议（BUY / SELL / HOLD），无权决定下单金额、撤销风控或触碰 API 私钥。
2. **退出可行性预检（Exit Feasibility）**：开仓前计算若遇逆向行情（如 -3% 跌幅），剩余持仓是否仍满足交易所最小成交额要求（`MIN_NOTIONAL`）。如不能合法卖出则坚决禁止买入。
3. **资金与精度安全**：涉及资金与数量计算全部强制使用 `decimal.Decimal`，拒绝二进制浮点数误差。
4. **实盘三重门禁**：`TRADING_MODE=live` + `ENABLE_LIVE_TRADING=true` + CLI 显式 `--confirm-live`，并在运行前执行全面的 preflight 规则检查。
5. **可审计性**：每根 K 线、每个决策信号（带唯一 `signal_id`）、每笔订单（唯一 `client_order_id`）与规则快照完整记录在 SQLite 中。

## 开发阶段实施完成情况

- [x] **P1 行情与项目骨架**：src-layout 架构、CCXT 行情接入、已收盘 1h K 线过滤、动态 exchangeInfo 规则解析与快照、SQLite 基础表。
- [x] **P2 策略与 Jev**：SMA/RSI/ATR 指标引擎、TypeSafe SDK 封装、BUY/SELL/HOLD、SMA 对照策略、无未来函数保证。
- [x] **P3 模拟交易**：Paper 账户撮合、滑点与手续费模拟、退出可行性计算、每日亏损熔断。
- [x] **P4 Spot Testnet**：测试网独立密钥、断网重连、重启对账与幂等保障。
- [x] **P5 受限实盘**：实盘三重门禁、Preflight 准入前置检查、资金 50 元上限拦截、UI/CLI 应急暂停开关。

## 仪表盘状态说明（重要）

当前 `dashboard/app.py` 是 **DEMO 演示页面，并非实时行情或实盘风控控制台**：

- BTC K 线使用程序生成的合成数据；ETH/SOL 报价、部分 KPI 和动作日志属于静态演示内容，不代表交易所真实结果。
- 页面的 Paper 余额与风险状态仅保存在当前 Streamlit 会话中，不保证重启持久化。
- 页面中的暂停/恢复按钮只控制这个会话内的风险管理实例，**不能暂停其他进程、测试网或真实交易**。
- 请勿根据演示行情、延迟数字或页面上的控制按钮判断实盘交易状态。真实交易运行前必须单独完成端到端执行、对账与安全测试。

## 离线决策事件录制与回放（实验性）

借鉴 [jev-market-reflex](https://github.com/zzsong1023/jev-market-reflex) 的「录制 → 离线回放」分离设计，增加了 **只读 JSONL 决策事件模块**。目前是独立的开发者 API，尚未接入自动交易主循环或 Streamlit；不会自动记录订单、连接交易所或执行交易。严格限制保存字段，不写入模型原始输出、用户账户或 API 密钥。

```python
from pathlib import Path
from jevquant.recording import make_decision_frame, write_recording, read_recording

frame = make_decision_frame(
    captured_at_utc="2026-10-11T15:00:00Z",
    candle_close_utc="2026-10-11T14:00:00Z",
    symbol="BTC/USDT",
    signal_id="example-001",
    action="HOLD",
    reason_code="REPLAY_EXAMPLE",
    confidence=None,
    source_mode="demo",
)
write_recording(Path("data/demo-recording.jsonl"), [frame])
print(list(read_recording(Path("data/demo-recording.jsonl"))))
```

录制文件使用排他创建，不覆盖已有文件。回放只解析并校验数据，不进行模型推理和真实/模拟下单。生产环境引入历史回放时必须保留来源标识、隔离交易执行器，并对文件权限、文件大小和保留周期做额外限制。

## 常用命令

```powershell
# 1. 激活虚拟环境 (Python 3.13)
.\.venv\Scripts\activate

# 2. 运行全量测试套件 (31 个自动化测试)
pytest -v

# 3. 运行公共行情与规则探测 (零 API Key 需求)
python -m jevquant.exchange

# 4. 启动 Jev Market Reflex 监控仪表盘 (端口 8501)
streamlit run dashboard/app.py
```
