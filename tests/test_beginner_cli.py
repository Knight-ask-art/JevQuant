"""Beginner commands must never connect to an exchange or expose credentials."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jevquant import __main__ as cli


@pytest.mark.parametrize("key", [
    "TYPESAFE_API_KEY",
    "BINANCE_API_KEY", "BINANCE_API_SECRET",
    "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET",
])
def test_demo_clears_all_api_credentials(monkeypatch, key):
    monkeypatch.setenv(key, "DO_NOT_LEAK_THIS_SECRET")
    env = cli.demo_environment()
    assert env[key] == ""
    assert env["TRADING_MODE"] == "paper"
    assert env["ENABLE_LIVE_TRADING"] == "false"
    assert env["LIVE_INITIAL_CAPITAL_USDT"] == ""


def test_demo_ignores_unsafe_live_environment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-key")
    app = tmp_path / "app.py"
    app.write_text("pass", encoding="utf-8")
    monkeypatch.setattr(cli, "dashboard_path", lambda: app)
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    launch = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli.subprocess, "run", launch)

    assert cli.main(["demo", "--port", "8502"]) == 0
    launch.assert_called_once()
    args, kwargs = launch.call_args
    command = args[0]
    assert command[:3] == [cli.sys.executable, "-m", "streamlit"]
    assert "--server.address" in command
    assert command[command.index("--server.address") + 1] == "127.0.0.1"
    assert command[command.index("--server.port") + 1] == "8502"
    assert kwargs["env"]["TRADING_MODE"] == "paper"
    assert kwargs["env"]["ENABLE_LIVE_TRADING"] == "false"
    assert kwargs["env"]["TYPESAFE_API_KEY"] == ""


def test_demo_rejects_invalid_port_without_launch(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(cli.subprocess, "run", launch)
    assert cli.main(["demo", "--port", "0"]) == 2
    assert cli.main(["demo", "--port", "65536"]) == 2
    launch.assert_not_called()


def test_demo_helpfully_reports_missing_streamlit(monkeypatch, capsys):
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _: None)
    launch = Mock()
    monkeypatch.setattr(cli.subprocess, "run", launch)
    assert cli.main(["demo"]) == 1
    launch.assert_not_called()
    assert ".[ui]" in capsys.readouterr().out


def test_doctor_does_not_print_credentials(monkeypatch, tmp_path: Path, capsys):
    app = tmp_path / "app.py"
    app.write_text("pass", encoding="utf-8")
    monkeypatch.setattr(cli, "dashboard_path", lambda: app)
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("BINANCE_API_KEY", "secret-api-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret-api-secret")
    assert cli.main(["doctor"]) == 0
    result = capsys.readouterr().out
    assert "当前配置模式：live" in result
    assert "demo 命令仍会强制安全演示模式" in result
    assert "secret-api-key" not in result
    assert "secret-api-secret" not in result


def test_doctor_reports_invalid_configuration_without_echoing_values(monkeypatch, tmp_path: Path, capsys):
    app = tmp_path / "app.py"
    app.write_text("pass", encoding="utf-8")
    monkeypatch.setattr(cli, "dashboard_path", lambda: app)
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setenv("TRADING_MODE", "secret-accidental-value")
    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "当前环境配置无效" in output
    assert "secret-accidental-value" not in output
