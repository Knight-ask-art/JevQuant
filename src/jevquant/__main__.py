"""Safe, beginner-friendly local entry point.

The demo command never initializes an exchange adapter or an order executor.
It always runs the existing synthetic-data dashboard in paper mode.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional


def demo_environment() -> dict[str, str]:
    """Isolate the demo from any trading credentials/settings in the user's .env."""
    environment = os.environ.copy()
    environment.update({
        "TRADING_MODE": "paper",
        "ENABLE_LIVE_TRADING": "false",
        "EXCHANGE": "binance",
        "SYMBOL": "BTC/USDT",
        "TIMEFRAME": "1h",
        "PAPER_INITIAL_BALANCE_USDT": "7.00",
        "LIVE_INITIAL_CAPITAL_USDT": "",
        "TYPESAFE_API_KEY": "",
        "BINANCE_API_KEY": "",
        "BINANCE_API_SECRET": "",
        "BINANCE_TESTNET_API_KEY": "",
        "BINANCE_TESTNET_API_SECRET": "",
        "JEV_MODEL": "jev-latest",
        "JEV_MAX_MONTHLY_COST_USD": "0.50",
    })
    return environment


def dashboard_path() -> Path:
    # The documented installation is editable, so the dashboard lives in
    # the checked-out repository even when invoked from another directory.
    return Path(__file__).resolve().parents[2] / "dashboard" / "app.py"


def doctor() -> int:
    """Check local prerequisites without network, API calls or order submission."""
    print("JevQuant 环境检查（只读，不联网，不下单）")
    okay = True
    if (3, 10) <= sys.version_info[:2] < (3, 14):
        print(f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        print("[FAIL] 需要 Python 3.10–3.13")
        okay = False

    if importlib.util.find_spec("streamlit") is None:
        print('[FAIL] 未安装界面依赖。运行：python -m pip install -e ".[ui]"')
        okay = False
    else:
        print("[OK] Streamlit 已安装")

    if dashboard_path().is_file():
        print("[OK] 找到本地演示界面")
    else:
        print("[FAIL] 找不到 dashboard/app.py。请从源码仓库使用 editable 安装。")
        okay = False

    try:
        from jevquant.config import load_config_from_env
        cfg = load_config_from_env()
    except (ValueError, TypeError):
        # Pydantic errors may quote environment values, including secrets.
        print("[WARN] 当前环境配置无效；请检查 .env 中的字段。不会显示配置值。")
        okay = False
    else:
        print(f"[OK] 当前配置模式：{cfg.trading_mode}")
        if cfg.trading_mode != "paper":
            print("[WARN] 当前配置并非 paper；demo 命令仍会强制安全演示模式。")

    print("提示：演示页面使用合成行情；无需 API Key，也不会运行真实交易。")
    return 0 if okay else 1


def demo(port: int) -> int:
    if not 1 <= port <= 65535:
        print("[FAIL] 端口必须在 1–65535 之间。", file=sys.stderr)
        return 2

    if importlib.util.find_spec("streamlit") is None:
        print('[FAIL] 需要安装界面依赖：python -m pip install -e ".[ui]"')
        return 1

    app = dashboard_path()
    if not app.is_file():
        print("[FAIL] 找不到 dashboard/app.py。请从仓库执行 editable 安装。")
        return 1

    print("启动 JevQuant 新手演示：仅合成行情 / Paper / 无 API Key / 不下单")
    print(f"本机访问：http://127.0.0.1:{port} （Ctrl+C 结束）")
    command = [
        sys.executable, "-m", "streamlit", "run", str(app),
        "--server.address", "127.0.0.1",
        "--server.port", str(port),
    ]
    try:
        return subprocess.run(command, env=demo_environment(), check=False).returncode
    except KeyboardInterrupt:
        return 130
    except OSError:
        print("[FAIL] 无法启动演示界面。请检查 Python/Streamlit 安装。")
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jevquant",
        description="JevQuant 新手工具（默认无任何下单权限）",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="只读检查 Python、界面和配置")
    demo_parser = sub.add_parser("demo", help="启动无密钥、离线数据演示（仅本机）")
    demo_parser.add_argument("--port", type=int, default=8501, help="本机端口（默认 8501）")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor()
    return demo(args.port)


if __name__ == "__main__":
    sys.exit(main())
