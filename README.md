# JevQuant

基于 **TypeSafe Jev + 币安现货 BTC/USDT** 的轻量级自动交易研究项目。用真实公开行情计算技术指标，由 Jev 生成结构化交易建议，经独立风控审批后由统一订单接口支持模拟盘（Paper）、测试网（Testnet）与受限小额实盘。

本金规划上限为 50 元人民币等值 USDT，不使用杠杆、不自动充值、不提现。第一目标是架构正确、可测试、可审计。

## 新手快速开始：不需要 API Key，不需要交易所账户

**先体验演示，再考虑策略和交易。** 以下步骤只启动合成行情演示界面；它不会调用币安下单接口，不需要资金，也不代表实时交易表现。

首次安装请在项目根目录操作（Windows PowerShell）：

```powershell
git clone https://github.com/Knight-ask-art/JevQuant.git
cd JevQuant
py -3.13 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install -e ".[ui]"
python -m jevquant doctor
python -m jevquant demo
```

macOS / Linux（需先安装 Python 3.10–3.13）：

```bash
git clone https://github.com/Knight-ask-art/JevQuant.git
cd JevQuant
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[ui]"
python -m jevquant doctor
python -m jevquant demo
```

浏览器访问 **http://127.0.0.1:8501** 即可查看演示。终端按 **Ctrl+C** 结束。首次演示**不需要创建 `.env`**。如果 8501 端口被占用，可改用 `python -m jevquant demo --port 8502`。

| 命令 | 用途 | 是否发起真实交易 |
|---|---|---|
| `python -m jevquant doctor` | 检查 Python 版本、界面依赖和本地配置，提供修复提示；不连接网络 | 否 |
| `python -m jevquant demo` | 强制安全演示配置，仅在本机启动合成行情仪表盘 | 否 |
| `python -m jevquant --help` | 查看新手命令 | 否 |

**注意**：无论本地 `.env` 是否配置 `TRADING_MODE=live` 或密钥，`demo` 都会覆盖交易模式为 `paper` 并清空传给演示子进程的项目 API 凭据。此命令**不会停止或影响其他已经运行的实盘进程**。

### 常见问题

- `py -3.13` 不存在：安装 Python 3.10–3.13，或将该命令替换为实际可用的 Python 解释器。
- `ModuleNotFoundError: No module named 'jevquant'`：确认在仓库根目录激活虚拟环境后执行 `python -m pip install -e ".[ui]"`。
- `Streamlit 未安装`：重新执行 `python -m pip install -e ".[ui]"`。
- 端口被占用：用 `--port 8502` 指定其他空闲端口。
- `doctor` 提示配置有误：检查 `.env` 中交易模式、数值等设置；**不用在聊天、Issue 或截图中公开 API Key**。
- 想运行自动实盘：此快速开始**不包含自动交易主循环**，请先阅读安全说明并完成测试网端到端验证，不能将 Demo 当成实盘。

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

## 新手交互式模拟交易练习（不需要 API Key）

本项目的 Streamlit 界面增加了 **「模拟交易练习 / Paper Practice」** 标签页，专门用来熟悉虚拟买卖与风控：

1. 在仓库根目录安装开发界面依赖：`python -m pip install -e ".[dev]"`。
2. 启动：`streamlit run dashboard/app.py`，在浏览器里打开 **「模拟交易练习」**。
3. 点击 **开始模拟练习**，获得 **7 USDT 虚拟资金**和一段**完全虚构**的 BTC/USDT 价格序列。
4. 尝试 **模拟买入**、**下一根合成 K 线**、**模拟卖出**；页面会显示模拟持仓、资产、盈亏及风控拒绝的中文解释。点击 **结束并重置练习**可以从头再来。

**边界非常重要**：这是交互教学，不是 Jev AI 自动下单或历史回测；只用虚构价格和示例成交额规则（最低 5 USDT），不发起任何交易所订单或网络行情查询。该练习独立于监控页的 Paper 余额，不影响正在运行的 Testnet/Live 进程。真实交易所的规则可能不同，练习结果不能用于评估交易策略盈利能力。

演示练习复用 `PaperAccount` 的手续费/滑点计算与 `RiskManager` 的买入审批；卖出前额外检查示例最低成交额，避免将无法执行的示例卖出标为成功。虚拟数据和交易记录只保存在当前 Streamlit 浏览器会话，重新加载服务或重置练习可能清空记录。

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
