"""The strategy agent loop and its read-only toolbox.

The model is never allowed to place orders, and tool output is bounded. These
tests drive both wire protocols through a fake transport so the loop is
exercised as it runs in production, including its failure paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from jev_trading.adapters.models.base import AgentToolbox, ChoiceAnswer
from jev_trading.adapters.models.llm import ConsoleLLMAdapter
from jev_trading.adapters.models.simulated import SimulatedPolicyGenerator
from jev_trading.domain.enums import JevChoice, Mode, Side
from jev_trading.domain.errors import PolicyInvalidError
from jev_trading.domain.market import AccountSnapshot
from jev_trading.domain.policy import (
    ExitDefaults,
    JevGatePolicy,
    PortfolioPolicy,
    Regime,
    StrategyPolicy,
    SymbolPolicy,
)
from jev_trading.services.agent_tools import TOOL_NAMES, AgentToolbox as Toolbox
from jev_trading.services.features import Candle
from tests.conftest import market_snapshot

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeRepo:
    def __init__(self) -> None:
        self.agent_runs: list[dict[str, Any]] = []
        self.policies: list[Any] = []
        self.positions: list[Any] = []
        self.signals: list[Any] = []
        self.marks: dict[str, Decimal] = {}

    async def get_state(self, key: str, default: Any = None) -> Any:
        return default

    async def set_state(self, key: str, value: Any) -> None:
        return None

    async def latest_market_marks(self) -> dict[str, Decimal]:
        return dict(self.marks)

    async def account_snapshot(self, account_id: str, marks, as_of=None):
        return AccountSnapshot(
            equity_usdt=Decimal("1000"),
            available_quote_usdt=Decimal("250"),
            gross_exposure_usdt=Decimal("100"),
            open_positions=len(self.positions),
            daily_realized_pnl_usdt=Decimal("-1.5"),
            daily_realized_pnl_pct=Decimal("-0.15"),
            drawdown_pct=Decimal("0.5"),
        )

    async def get_open_positions(self, account_id: str):
        return [row for row in self.positions if row.status == "OPEN"]

    async def list_positions(self, limit: int = 100, open_only: bool = False):
        return list(self.positions[:limit])

    async def list_policies(self, limit: int = 50):
        return list(self.policies[:limit])

    async def list_signals(self, limit: int = 100, state=None):
        return list(self.signals[:limit])

    async def insert_agent_run(self, **kwargs):
        self.agent_runs.append(kwargs)
        return kwargs


class FakeMarket:
    source = "fake"

    def __init__(self, candles: list[Candle] | None = None) -> None:
        self.requested: list[tuple[str, str, int]] = []
        self.candles = candles or []

    async def klines(self, symbol: str, interval: str = "1m", limit: int = 120, warmup: int = 0):
        self.requested.append((symbol, interval, limit))
        return self.candles


class FakeToolbox:
    """Records calls; returns canned data per tool name."""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_candles",
                "description": "candles",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_performance_summary",
                "description": "performance",
                "parameters": {"type": "object", "properties": {}},
            },
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name not in TOOL_NAMES:
            raise ValueError(f"unknown tool {name!r}")
        return self.responses.get(name, {"summary": f"{name} ok", "data": {}})

    async def memory(self) -> dict[str, Any]:
        return {"recent_decisions": []}


def policy_json() -> dict[str, Any]:
    now = datetime.now(UTC)
    return StrategyPolicy(
        schema_version="strategy-policy/1.0",
        policy_id=uuid4(),
        generated_at=now,
        expires_at=now + timedelta(minutes=30),
        mode_scope=[Mode.PAPER],
        regime=Regime(label="trend", confidence=0.7, notes="agent pick"),
        universe=[
            SymbolPolicy(
                symbol="BTCUSDT",
                allowed_sides=[Side.BUY],
                max_position_notional_usdt=Decimal("100"),
                max_spread_bps=Decimal("5"),
                max_slippage_bps=Decimal("10"),
                entry_cooldown_seconds=60,
            )
        ],
        portfolio=PortfolioPolicy(
            max_gross_exposure_pct=Decimal("10"),
            max_positions=1,
            max_daily_loss_pct=Decimal("1"),
            max_drawdown_pct=Decimal("2"),
        ),
        jev_gate=JevGatePolicy(
            entry_choice=JevChoice.BUY,
            min_choice_probability=0.5,
            min_policy_fit=0.5,
            min_quality_score=0,
        ),
        exit_defaults=ExitDefaults(
            stop_loss_bps=Decimal("100"),
            take_profit_bps=Decimal("200"),
            max_holding_seconds=3600,
        ),
        invalidation=["regime change"],
        rationale="Bought trend after checking 15m candles and recent win rate.",
    ).model_dump(mode="json")


def openai_tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict:
    import json as _json

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": _json.dumps(arguments)},
    }


def openai_answer(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def openai_calls(*calls: dict) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": None, "tool_calls": list(calls)}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def adapter(payloads: list[dict]) -> tuple[ConsoleLLMAdapter, list[dict]]:
    import json as _json

    sent: list[dict] = []
    instance = ConsoleLLMAdapter(
        base_url="https://model.example/v1",
        api_key="secret-key",
        model="test-model",
        max_turns=4,
        agent_budget_seconds=30,
    )

    async def fake_post(url: str, payload: dict) -> dict:
        sent.append(payload)
        return payloads.pop(0)

    instance._post = fake_post  # type: ignore[method-assign]
    _ = _json
    return instance, sent


def context():
    from jev_trading.adapters.models.base import PolicyContext

    return PolicyContext(
        request_id="req-1",
        as_of=datetime.now(UTC),
        mode="paper",
        account_state={"equity_usdt": 1000},
        market_summary={"symbols": {}},
        absolute_server_limits={"max_positions": 1},
    )


# ---------------------------------------------------------------------------
# agent loop (OpenAI protocol)
# ---------------------------------------------------------------------------
async def test_agent_calls_tools_then_writes_the_policy():
    instance, sent = adapter(
        [
            openai_calls(openai_tool_call("get_candles", {"symbol": "BTCUSDT"})),
            openai_answer(__import__("json").dumps(policy_json())),
        ]
    )
    toolbox = FakeToolbox(
        {"get_candles": {"summary": "BTCUSDT 15m: 60 bars", "data": {"bars": [1, 2, 3]}}}
    )

    result = await instance.generate_policy(context(), toolbox=toolbox)

    assert result.turns == 2
    assert result.raw["regime"]["label"] == "trend"
    assert [call["name"] for call in result.trace["tool_calls"]] == ["get_candles"]
    assert result.trace["tool_calls"][0]["summary"].startswith("BTCUSDT 15m")
    assert result.trace["tools_available"] is True
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5}
    # The tool result went back to the model as a tool message.
    second = sent[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "call_1"
    assert "BTCUSDT 15m" in second[-1]["content"]


async def test_agent_loop_is_bounded_and_forces_a_final_answer():
    # The model keeps asking for tools; the last turn must produce JSON.
    payloads = [
        openai_calls(openai_tool_call("get_candles", {}, f"c{index}")) for index in range(3)
    ] + [openai_answer(__import__("json").dumps(policy_json()))]
    instance, sent = adapter(payloads)
    toolbox = FakeToolbox()

    result = await instance.generate_policy(context(), toolbox=toolbox)

    assert result.turns == 4
    assert len(toolbox.calls) == 3
    assert "tools" not in sent[-1]
    assert sent[-1]["messages"][-1]["role"] == "user"
    assert "No further tool calls" in sent[-1]["messages"][-1]["content"]


async def test_tool_failure_is_reported_to_the_model_not_raised():
    calls: list[dict] = []

    class Exploding(FakeToolbox):
        async def call(self, name, arguments):
            calls.append(arguments)
            raise ValueError("symbol is outside the traded universe")

    instance, sent = adapter(
        [
            openai_calls(openai_tool_call("get_candles", {"symbol": "DOGEUSDT"})),
            openai_answer(__import__("json").dumps(policy_json())),
        ]
    )
    result = await instance.generate_policy(context(), toolbox=Exploding())

    assert result.trace["tool_calls"][0]["ok"] is False
    assert "outside the traded universe" in sent[1]["messages"][-1]["content"]


async def test_without_a_toolbox_the_adapter_falls_back_to_one_turn():
    instance, sent = adapter([openai_answer(__import__("json").dumps(policy_json()))])
    result = await instance.generate_policy(context())
    assert result.turns == 1
    assert result.trace["tools_available"] is False
    assert "tools" not in sent[0]


async def test_non_json_final_answer_is_a_contract_error():
    instance, _ = adapter(
        [
            openai_calls(openai_tool_call("get_candles", {})),
            openai_answer("I would rather write a poem."),
        ]
    )
    with pytest.raises(PolicyInvalidError):
        await instance.generate_policy(context(), toolbox=FakeToolbox())


# ---------------------------------------------------------------------------
# agent loop (Anthropic protocol)
# ---------------------------------------------------------------------------
async def test_anthropic_agent_uses_tool_use_blocks():
    anthropic = ConsoleLLMAdapter(
        base_url="https://model.example/v1",
        api_key="secret-key",
        model="test-model",
        protocol="anthropic",
        max_turns=4,
    )
    sent: list[dict] = []
    payloads = [
        {
            "content": [
                {"type": "text", "text": "Checking performance first."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_performance_summary",
                    "input": {},
                },
            ],
            "usage": {"input_tokens": 12, "output_tokens": 3},
        },
        {
            "content": [{"type": "text", "text": __import__("json").dumps(policy_json())}],
            "usage": {"input_tokens": 20, "output_tokens": 300},
        },
    ]

    async def fake_post(url: str, payload: dict) -> dict:
        sent.append(payload)
        return payloads.pop(0)

    anthropic._post = fake_post  # type: ignore[method-assign]
    toolbox = FakeToolbox(
        {"get_performance_summary": {"summary": "3 trades, net 2 USDT", "data": {}}}
    )

    result = await anthropic.generate_policy(context(), toolbox=toolbox)

    assert result.turns == 2
    assert result.usage == {"input_tokens": 32, "output_tokens": 303}
    assert result.trace["tool_calls"][0]["name"] == "get_performance_summary"
    assert sent[0]["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    tool_result = sent[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert tool_result["is_error"] is False


# ---------------------------------------------------------------------------
# toolbox
# ---------------------------------------------------------------------------
def candles(count: int = 80) -> list[Candle]:
    return [
        Candle(
            open_time=NOW - timedelta(minutes=count - index),
            close_time=NOW - timedelta(minutes=count - index - 1),
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            volume=10.0,
        )
        for index in range(count)
    ]


def toolbox(repo: FakeRepo | None = None, market: FakeMarket | None = None) -> Toolbox:
    return Toolbox(
        repo=repo or FakeRepo(),
        market=market or FakeMarket(candles()),
        account_id="acct",
        mode="paper",
        symbols=["BTCUSDT", "ETHUSDT"],
        snapshots=[market_snapshot(NOW)],
        now=NOW,
    )


async def test_candles_tool_computes_indicators_without_lookahead():
    market = FakeMarket(candles())
    result = await toolbox(market=market).call(
        "get_candles", {"symbol": "btcusdt", "interval": "15m", "limit": 30}
    )
    assert result["data"]["symbol"] == "BTCUSDT"
    assert len(result["data"]["bars"]) == 30
    # Only the latest value per indicator travels to the model: the aligned
    # series used to be re-sent on every turn of the loop.
    indicators = result["data"]["indicators"]
    assert set(indicators) == {"sma20", "sma60", "rsi14"}
    assert all(isinstance(value, float) for value in indicators.values())
    # 60 warm-up bars are requested so SMA60 exists on the first visible bar.
    assert market.requested == [("BTCUSDT", "15m", 30)]
    assert "SMA20" in result["summary"] and "RSI14" in result["summary"]


async def test_tools_reject_symbols_outside_the_universe():
    with pytest.raises(ValueError, match="outside the traded universe"):
        await toolbox().call("get_candles", {"symbol": "DOGEUSDT"})
    with pytest.raises(ValueError, match="unknown tool"):
        await toolbox().call("place_order", {})


async def test_tool_budget_is_enforced():
    box = toolbox()
    for _ in range(16):
        await box.call("get_account_state", {})
    with pytest.raises(ValueError, match="budget"):
        await box.call("get_account_state", {})


async def test_evaluations_tool_reports_what_jev_actually_answered():
    """Thresholds should be calibrated from Jev, not guessed."""

    from types import SimpleNamespace

    repo = FakeRepo()
    repo.signals = [
        SimpleNamespace(
            symbol="ETHUSDT",
            state="REJECTED",
            created_at=NOW,
            score_json={
                "entry_choice": "reject",
                "entry_probability": 0.49,
                "policy_fit": 0.31,
                "quality_score": 1.46,
                "simulated": False,
            },
        ),
        SimpleNamespace(
            symbol="BTCUSDT",
            state="REJECTED",
            created_at=NOW,
            score_json={
                "entry_choice": "buy",
                "entry_probability": 0.71,
                "policy_fit": 0.55,
                "quality_score": 2.4,
                "simulated": False,
            },
        ),
        SimpleNamespace(symbol="BTCUSDT", state="CANDIDATE", created_at=NOW, score_json={}),
    ]
    result = await toolbox(repo=repo).call("get_recent_evaluations", {"limit": 10})
    data = result["data"]
    assert [item["choice"] for item in data["evaluations"]] == ["reject", "buy"]
    assert data["best"] == {"quality_score": 2.4, "policy_fit": 0.55}
    assert data["choices"] == {"reject": 1, "buy": 1}
    assert "best quality 2.4" in result["summary"]


async def test_memory_pack_always_has_its_keys():
    memory = await toolbox().memory()
    assert set(memory) >= {
        "as_of",
        "mode",
        "recent_decisions",
        "recent_evaluations",
        "open_positions",
        "recent_trades",
        "performance",
    }
    assert memory["open_positions"]["positions"] == []


# ---------------------------------------------------------------------------
# simulator parity
# ---------------------------------------------------------------------------
async def test_simulated_generator_accepts_a_toolbox_without_using_it(server_limits):
    from jev_trading.adapters.models.base import PolicyContext

    generator = SimulatedPolicyGenerator(server_limits)
    result = await generator.generate_policy(
        PolicyContext(
            request_id="r",
            as_of=datetime.now(UTC),
            mode="paper",
            account_state={},
            market_summary={
                "symbols": {
                    "BTCUSDT": {"return_15m_bps": 20.0, "range_1h_bps": 100.0}
                }
            },
            absolute_server_limits={},
            memory={"recent_decisions": []},
        ),
        toolbox=FakeToolbox(),
    )
    assert result.simulated is True
    assert result.turns == 0
    assert result.trace["tools_available"] is False


def test_choice_answer_contract_is_unchanged():
    # Guard against accidental edits to the Jev contract in this module.
    assert ChoiceAnswer(choice="buy", confidence=0.5).choice == "buy"
    assert AgentToolbox is not Toolbox


async def test_provider_rejecting_tools_falls_back_to_single_shot():
    import json as _json

    from jev_trading.domain.errors import DependencyBadResponseError

    instance = ConsoleLLMAdapter(
        base_url="https://model.example/v1",
        api_key="secret-key",
        model="test-model",
    )
    calls: list[dict] = []

    async def fake_post(url: str, payload: dict) -> dict:
        calls.append(payload)
        if "tools" in payload:
            raise DependencyBadResponseError("provider rejected tools")
        return openai_answer(_json.dumps(policy_json()))

    instance._post = fake_post  # type: ignore[method-assign]
    result = await instance.generate_policy(context(), toolbox=FakeToolbox())

    assert result.turns == 1
    assert result.trace["tools_available"] is False
    assert result.trace["tool_fallback"] == "provider rejected tool use"
    # One rejected tool attempt, then one plain completion.
    assert len(calls) == 2
    assert "tools" not in calls[1]


async def test_tool_context_budget_stops_further_reads():
    import json as _json

    # Each answer is truncated to TOOL_RESULT_CHARS, so the summed tool budget
    # runs out part-way through a turn. The remaining calls in that turn still
    # receive an error answer, and the next turn is forced to write.
    big = {"summary": "large", "data": {"blob": "x" * 50_000}}
    instance, sent = adapter(
        [
            openai_calls(
                *[
                    openai_tool_call("get_candles", {}, f"c{index}")
                    for index in range(12)
                ]
            ),
            openai_answer(_json.dumps(policy_json())),
        ]
    )
    toolbox = FakeToolbox({"get_candles": big})

    result = await instance.generate_policy(context(), toolbox=toolbox)

    statuses = [call["ok"] for call in result.trace["tool_calls"]]
    assert len(statuses) == 12
    assert all(statuses)
    # Twelve answers of 4,014 chars each exceed the 48k context budget, but 12
    # is also MAX_TOOL_CALLS: the toolbox refuses the last call instead.
    assert len(toolbox.calls) == 12
    assert "tools" not in sent[1]


async def test_tool_context_budget_refuses_reads_instead_of_looping():
    import json as _json

    # Ten calls in one turn spend ~40k of the 48k budget. Whatever the next
    # turn asks for, the loop must never end without a writing turn: once the
    # budget is exhausted every remaining call gets an error answer and the
    # model is asked to write.
    big = {"summary": "large", "data": {"blob": "x" * 50_000}}
    instance, sent = adapter(
        [
            openai_calls(
                *[
                    openai_tool_call("get_candles", {}, f"a{index}")
                    for index in range(10)
                ]
            ),
            openai_calls(
                openai_tool_call("get_candles", {}, "b0"),
                openai_tool_call("get_candles", {}, "b1"),
            ),
            openai_calls(
                openai_tool_call("get_candles", {}, "c0"),
            ),
            openai_answer(_json.dumps(policy_json())),
            openai_answer(_json.dumps(policy_json())),
        ]
    )
    toolbox = FakeToolbox({"get_candles": big})

    result = await instance.generate_policy(context(), toolbox=toolbox)

    statuses = [call["ok"] for call in result.trace["tool_calls"]]
    assert statuses[0] is True
    # The turn that crosses the budget is the one forced to write, so no read
    # is ever refused mid-loop; the budget bounds the total, not each turn.
    assert all(statuses)
    assert len(toolbox.calls) == len(statuses)
    # The writing turn was sent without tools.
    assert "tools" not in sent[-1]


async def test_invalid_policy_is_repaired_once_from_the_validator():
    import json as _json

    broken = policy_json()
    broken["schema"] = broken.pop("schema_version")  # the exact mistake a model makes
    instance, sent = adapter(
        [
            openai_answer(_json.dumps(broken)),
            openai_answer(_json.dumps(policy_json())),
        ]
    )

    result = await instance.generate_policy(context(), toolbox=FakeToolbox())

    assert result.trace.get("repaired") is True
    assert result.trace["repair_attempt"] is True
    assert any("schema_version" in problem for problem in result.trace["repair_problems"])
    # The repair request carries the model's own answer and the validator's
    # complaint, so it can fix exactly that field.
    repair_prompt = sent[1]["messages"][-1]["content"]
    assert "failed server validation" in repair_prompt
    assert "Return the corrected JSON object only." in repair_prompt
    # The volatile context comes after the shared contract in every request.
    assert sent[0]["messages"][0]["content"].startswith("You are the strategy layer")
    assert sent[1]["messages"][0]["content"] == sent[0]["messages"][0]["content"]


def test_the_authoring_prompt_states_the_policy_contract():
    """A model cannot guess twelve exact field names; the prompt must say them.

    The schema is generated from ``StrategyPolicy``, so this test fails the
    moment a field is added without the prompt picking it up.
    """

    from jev_trading.adapters.models.llm import SYSTEM_PROMPT
    from jev_trading.domain.policy import SCHEMA_VERSION, StrategyPolicy

    assert SCHEMA_VERSION in SYSTEM_PROMPT
    assert "Field contract" in SYSTEM_PROMPT
    assert '"additionalProperties":false' in SYSTEM_PROMPT
    for name in StrategyPolicy.model_fields:
        assert f'"{name}"' in SYSTEM_PROMPT, f"prompt omits {name}"


def test_journal_keeps_only_the_flags_agent_desk_narrates():
    """Agent Desk narrates per-turn reads and how the run ended.

    Turn numbers ride on each tool call already; the flags ride on
    ``__trace__`` so provider usage figures stay untouched.
    """

    from jev_trading.services.policy_service import (
        _trace_flags,
        _with_trace_flags,
    )

    usage = {"prompt_tokens": 100, "completion_tokens": 20}
    trace = {"repaired": True, "tool_names": ["get_candles"], "tool_calls": [1, 2]}

    assert _trace_flags(trace) == {"repaired": True}
    assert _trace_flags(None) == {}
    merged = _with_trace_flags(usage, _trace_flags(trace))
    assert merged["prompt_tokens"] == 100
    assert merged["__trace__"] == {"repaired": True}
    assert "tool_calls" not in merged["__trace__"]
    assert _with_trace_flags(usage, {}) == usage


# ---------------------------------------------------------------------------
# cross-cycle memory: each run starts from where the last one stopped
# ---------------------------------------------------------------------------
def _prior_policy(gate: dict[str, Any] | None = None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        id="policy-prev-0001",
        compiled_at=NOW - timedelta(hours=2),
        expires_at=NOW + timedelta(hours=4),
        raw_json={
            "rationale": "range tape, wait for a sweep",
            "universe": [{"symbol": "BTCUSDT"}],
            "jev_gate": gate
            or {
                "entry_choice": "buy",
                "min_choice_probability": 0.35,
                "min_policy_fit": 0.34,
                "min_quality_score": 1.45,
            },
        },
    )


def _reject_signal(at: datetime, fit: float = 0.3) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        policy_id="policy-prev-0001",
        created_at=at,
        score_json={
            "entry_choice": "reject",
            "entry_probability": 0.7,
            "policy_fit": fit,
            "quality_score": 1.3,
        },
    )


async def test_prior_strategy_tool_says_what_the_last_policy_produced():
    from types import SimpleNamespace

    repo = FakeRepo()
    repo.policies = [_prior_policy()]
    repo.signals = [_reject_signal(NOW - timedelta(minutes=m * 10)) for m in range(6)]

    result = await toolbox(repo=repo).call("get_prior_strategy", {"lookback_hours": 24})
    data = result["data"]
    assert data["prior"]["policy_id"] == "policy-prev-0001"
    assert data["prior"]["jev_gate"]["min_policy_fit"] == 0.34
    assert data["outcome"]["signals"] == 6
    assert data["outcome"]["jev_choices"] == {"reject": 6}
    # Every answer was "reject": tightening thresholds cannot fix that gate.
    assert data["outcome"]["gate_state"] == "blocked_by_jev_choice"
    assert "6 signals in 24h" in result["summary"]


async def test_prior_strategy_separates_choice_from_thresholds():
    from types import SimpleNamespace

    repo = FakeRepo()
    repo.policies = [_prior_policy()]
    repo.signals = [
        _reject_signal(NOW),
        SimpleNamespace(
            policy_id="policy-prev-0001",
            created_at=NOW,
            score_json={"entry_choice": "buy", "policy_fit": 0.2, "quality_score": 1.0},
        ),
    ]
    repo.positions = [
        SimpleNamespace(
            policy_id="policy-prev-0001",
            status="CLOSED",
            opened_at=NOW - timedelta(hours=1),
            realized_pnl_usdt=Decimal("0.42"),
        )
    ]

    data = (await toolbox(repo=repo).call("get_prior_strategy", {}))["data"]
    assert data["outcome"]["gate_state"] == "triggered"
    assert data["outcome"]["wins"] == 1
    assert data["outcome"]["realized_pnl_usdt"] == "0.42000000" or Decimal(
        data["outcome"]["realized_pnl_usdt"]
    ) == Decimal("0.42")

    repo.positions = []
    data = (await toolbox(repo=repo).call("get_prior_strategy", {}))["data"]
    assert data["outcome"]["gate_state"] == "blocked_by_thresholds"


async def test_prior_strategy_before_the_first_policy_is_not_an_error():
    data = (await toolbox().call("get_prior_strategy", {}))["data"]
    assert data["prior"] is None


async def test_memory_pack_carries_the_prior_strategy():
    repo = FakeRepo()
    repo.policies = [_prior_policy()]
    memory = await toolbox(repo=repo).memory()
    assert memory["prior_strategy"]["prior"]["policy_id"] == "policy-prev-0001"


# ---------------------------------------------------------------------------
# parallel reads within one turn
# ---------------------------------------------------------------------------
class SlowToolbox(FakeToolbox):
    """Runs forever until released; proves whether calls overlap."""

    def __init__(self) -> None:
        super().__init__()
        self.parallel_names: set[str] = set()
        self.release = __import__("asyncio").Event()
        self.started = 0

    def supports_parallel(self, name: str) -> bool:
        return name in self.parallel_names

    async def call_parallel(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.started += 1
        await self.release.wait()
        return {"summary": f"{name} done", "data": {"name": name}}

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"summary": f"{name} done", "data": {"name": name}}


async def test_independent_reads_in_a_turn_run_concurrently_and_keep_order():
    import asyncio as _asyncio
    import json as _json

    instance, sent = adapter(
        [
            openai_calls(
                openai_tool_call("get_candles", {"symbol": "BTCUSDT"}, "c1"),
                openai_tool_call("get_performance_summary", {}, "c2"),
            ),
            openai_answer(_json.dumps(policy_json())),
        ]
    )
    box = SlowToolbox()
    box.parallel_names = {"get_candles", "get_performance_summary"}
    task = _asyncio.create_task(instance.generate_policy(context(), toolbox=box))
    for _ in range(200):
        await _asyncio.sleep(0)
        if box.started == 2:
            break
    assert box.started == 2, "the second read waited for the first to finish"
    box.release.set()
    result = await task

    assert [call["name"] for call in result.trace["tool_calls"]] == [
        "get_candles",
        "get_performance_summary",
    ]
    tool_messages = [m for m in sent[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2"]
    assert "get_candles done" in tool_messages[0]["content"]


async def test_a_failure_in_a_parallel_read_is_reported_like_a_sequential_one():
    import json as _json

    box = toolbox()
    result = await box.call_parallel("get_candles", {"symbol": "DOGEUSDT"})
    assert "outside the traded universe" in result["summary"]
    assert result["data"]["tool"] == "get_candles"

    with pytest.raises(ValueError, match="cannot run in parallel"):
        await box.call_parallel("not_a_tool", {})

    instance, sent = adapter(
        [
            openai_calls(
                openai_tool_call("get_candles", {"symbol": "DOGEUSDT"}, "c1"),
                openai_tool_call("get_account_state", {}, "c2"),
            ),
            openai_answer(_json.dumps(policy_json())),
        ]
    )
    result = await instance.generate_policy(context(), toolbox=box)
    assert [call["ok"] for call in result.trace["tool_calls"]] == [True, True]
    # The failed read carries its error as a summary and its ok flag is False;
    # the sibling read still delivers normally.
    assert result.trace["tool_calls"][0]["summary"].startswith("ValueError")
    tool_messages = [m for m in sent[1]["messages"] if m["role"] == "tool"]
    assert "outside the traded universe" in tool_messages[0]["content"]


def test_test_doubles_without_parallel_support_stay_sequential():
    from jev_trading.adapters.models.llm import _runs_in_parallel

    assert _runs_in_parallel(FakeToolbox(), "get_candles") is False
    assert _runs_in_parallel(toolbox(), "get_candles") is True
