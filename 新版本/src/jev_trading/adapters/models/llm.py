"""OpenCode Console and OpenCode Go LLM adapters.

Both speak the same transport.  The difference that matters is the
server-side *purpose guard*: OpenCode Go is documented as coding-agent
traffic, so ``trade_runtime`` is denied before any request is sent.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any

import httpx
from pydantic import ValidationError

from ...domain.errors import (
    DependencyBadResponseError,
    DependencyUnavailableError,
    PolicyInvalidError,
    ProviderRateLimitedError,
    PurposeNotAllowedError,
)
from ...domain.policy import StrategyPolicy
from .base import AgentToolbox, PolicyContext, PolicyGenerationResult

def _policy_schema_text() -> str:
    """The exact JSON Schema the authoring prompt embeds.

    Generated from ``StrategyPolicy`` so the prompt cannot drift from the
    server-side validator. Pydantic's ``title`` keys only restate the field
    names, so they are dropped to keep the prompt small.
    """

    schema = StrategyPolicy.model_json_schema()

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: strip(value) for key, value in node.items() if key != "title"}
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    return json.dumps(strip(schema), ensure_ascii=False, separators=(",", ":"))


POLICY_SCHEMA_TEXT = _policy_schema_text()

_POLICY_RULES = ("""You are the strategy layer of an automated trading system.
Return exactly one JSON object that validates against schema strategy-policy/1.0.

Rules:
- Output JSON only. No prose, no markdown, no code fences.
- Never propose limits above absolute_server_limits; they are hard caps.
- The universe you receive is a hard maximum. You decide which symbols to
  trade and may include a strict subset, each with its own limits; symbols
  outside it are discarded.
- mode_scope may only contain modes you were told are active.
- Calibrate jev_gate against what Jev actually returns: call
  get_recent_evaluations before choosing minimums. quality_score is a graded
  level that in practice sits far below 5, and policy_fit is a probability.
  A minimum above the observed values rejects every setup, so pick thresholds
  a genuinely good setup can clear.
- Start from memory.prior_strategy: it is the policy you wrote last and what
  it produced. Read its outcome.gate_state before changing anything:
  blocked_by_jev_choice means Jev never said buy, so tightening or loosening
  thresholds cannot help and the setup itself must change; blocked_by_thresholds
  means Jev wanted in but your minimums held it, so lower the failing leg;
  triggered means it traded, so judge it by the closed trades and PnL.
- Independent reads can be requested together in one turn (for example both
  symbols' candles plus the account); they run concurrently.
- Keep rationale under 400 words. Say which part of the prior outcome you kept
  and which you changed, and why.
- You never place orders and never see exchange credentials.

Agent protocol (when tools are available):
- You may call the read-only tools before writing the policy. Call one only
  when its answer would change a decision: inspect the timeframe you intend
  to trade, check how recent decisions actually performed, and compare with
  your own recent rationales.
- Do not repeat a call whose result you already have. Investigate, then decide.
- Tools only read state. They cannot place orders, move money, or widen the
  server limits.
- When you have enough evidence, stop calling tools and return the policy
  JSON object. Output JSON only.

Field contract. The JSON Schema below is generated from the server validator.
Every listed field is required, unknown fields are rejected, and enum values
must match exactly. Dates are ISO-8601 with a timezone; policy_id is a UUID
you choose; generated_at is now and expires_at is now plus the TTL you want.
"""
    + POLICY_SCHEMA_TEXT
)

# What a response must look like. It goes first in every request so the
# context that changes between cycles (limits, market, memory) follows it:
# a shared prefix is what prompt caching can reuse, and the market summary
# alone is several thousand tokens.
RESPONSE_CONTRACT = (
    _POLICY_RULES
    + "\n\nProvider note: this model may emit reasoning text before its "
    "answer. The final answer must still be exactly the JSON object."
)

SYSTEM_PROMPT = _POLICY_RULES

ALLOWED_PURPOSES = {
    "coding_agent",
    "strategy_authoring",
    "trade_runtime",
    "backtest_analysis",
}

# Total tool output fed back to the model in one authoring run. The per-call
# cap lives in the toolbox; this bounds the sum so a model that keeps asking
# cannot overflow a small context window.
MAX_TOOL_CONTEXT_CHARS = 48_000

# One tool answer. Everything a tool returns is re-sent with every later
# turn, so an oversized first result is paid for again on each turn.
TOOL_RESULT_CHARS = 4_000


class ConsoleLLMAdapter:
    provider = "opencode_console"
    allowed_purposes = ALLOWED_PURPOSES

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        protocol: str = "openai",
        purpose: str = "trade_runtime",
        timeout: float = 45.0,
        max_retries: int = 2,
        max_delay_ms: int = 30_000,
        max_turns: int = 6,
        agent_budget_seconds: float = 90.0,
    ) -> None:
        if not base_url or not api_key or not model:
            raise DependencyUnavailableError(
                "OpenCode Console is not fully configured (base_url, api_key, model)."
            )
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.protocol = protocol
        self.purpose = purpose
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_delay_ms = max_delay_ms
        # Agent loop bounds. Six turns is enough for a handful of focused
        # lookups; the wall-clock budget stops a slow provider from holding
        # the trading cycle open.
        self.max_turns = max(1, max_turns)
        self.agent_budget_seconds = max(5.0, agent_budget_seconds)
        self._client = httpx.AsyncClient(timeout=timeout)

    supports_tools = True

    # -- purpose guard ---------------------------------------------------
    def ensure_purpose(self, purpose: str) -> None:
        if purpose not in self.allowed_purposes:
            raise PurposeNotAllowedError(
                f"{self.provider} does not permit purpose {purpose!r}.",
                details={"provider": self.provider, "purpose": purpose},
            )

    # -- transport -------------------------------------------------------
    def _endpoint(self, path: str) -> str:
        if self.base_url.endswith("/v1") and path.startswith("/v1"):
            return self.base_url + path[len("/v1") :]
        return self.base_url + path

    def _headers(self) -> dict[str, str]:
        if self.protocol == "anthropic":
            return {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        return {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            "user-agent": "jev-trading/0.1 (+strategy-authoring)",
        }

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = await self._client.post(url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise DependencyUnavailableError(
                        "Model provider is unreachable.",
                        details={"reason": type(exc).__name__},
                    ) from exc
            else:
                if response.status_code in (401, 403):
                    raise DependencyUnavailableError(
                        "Model provider rejected the credential.",
                        details={"http_status": response.status_code},
                    )
                if response.status_code == 429:
                    retry_after = _retry_after_ms(response.headers.get("Retry-After"))
                    if attempt >= self.max_retries:
                        raise ProviderRateLimitedError(
                            "Model provider rate limit reached.", retry_after_ms=retry_after
                        )
                elif response.status_code >= 500:
                    if attempt >= self.max_retries:
                        raise DependencyUnavailableError(
                            "Model provider returned a server error.",
                            details={"http_status": response.status_code},
                        )
                elif response.status_code >= 400:
                    raise DependencyBadResponseError(
                        "Model provider rejected the request.",
                        details={"http_status": response.status_code},
                    )
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise DependencyBadResponseError(
                            "Model provider returned non-JSON data."
                        ) from exc

            attempt += 1
            await asyncio.sleep(self._backoff_seconds(attempt))

    def _backoff_seconds(self, attempt: int) -> float:
        ceiling = min(self.max_delay_ms, 500 * (2 ** attempt)) / 1000.0
        return random.uniform(0.0, ceiling)

    async def _chat(
        self, system: str, user: str, *, json_mode: bool = True
    ) -> tuple[str, dict[str, Any]]:
        if self.protocol == "anthropic":
            payload: dict[str, Any] = {
                "model": self.model,
                "max_tokens": 2048,
                "temperature": 0.2,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            data = await self._post(self._endpoint("/v1/messages"), payload)
            blocks = data.get("content") or []
            text = "".join(
                block.get("text", "") for block in blocks if isinstance(block, dict)
            )
            return text, dict(data.get("usage") or {})

        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            data = await self._post(self._endpoint("/v1/chat/completions"), payload)
        except DependencyBadResponseError:
            if not json_mode:
                raise
            payload.pop("response_format", None)
            data = await self._post(self._endpoint("/v1/chat/completions"), payload)
        choices = data.get("choices") or []
        if not choices:
            raise DependencyBadResponseError("Model response contained no choices.")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise DependencyBadResponseError("Model response content was not text.")
        return content, dict(data.get("usage") or {})

    async def generate_policy(
        self,
        context: PolicyContext,
        *,
        purpose: str | None = None,
        toolbox: AgentToolbox | None = None,
    ) -> PolicyGenerationResult:
        effective_purpose = purpose or self.purpose
        self.ensure_purpose(effective_purpose)
        started = time.perf_counter()
        user = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)
        trace: dict[str, Any] = {"tools_available": False, "tool_calls": []}
        turns = 0
        if toolbox is not None and self.supports_tools:
            specs = toolbox.specs()
            if specs:
                trace["tools_available"] = True
                trace["tool_names"] = [spec["name"] for spec in specs]
                try:
                    if self.protocol == "anthropic":
                        text, usage, trace, turns = await self._anthropic_agent(
                            SYSTEM_PROMPT, user, toolbox
                        )
                    else:
                        text, usage, trace, turns = await self._openai_agent(
                            SYSTEM_PROMPT, user, toolbox
                        )
                except DependencyBadResponseError:
                    # Some OpenAI-compatible proxies reject the tools field
                    # outright. Fall back to the single-shot contract rather
                    # than failing a cycle the provider could otherwise serve.
                    trace = {"tools_available": False, "tool_calls": []}
                    trace["tool_fallback"] = "provider rejected tool use"
                    turns = 0
        if not turns:
            text, usage = await self._chat(SYSTEM_PROMPT, user)
            turns = 1
        latency_ms = int((time.perf_counter() - started) * 1000)
        data = _extract_json(text)
        try:
            StrategyPolicy.model_validate(data)
        except ValidationError as exc:
            # Repair once, from the validator itself: a wrong field name or a
            # missing key otherwise costs the whole authoring run (~20k tokens
            # of investigation discarded) and the next cycle starts over.
            repaired, usage = await self._repair_policy(
                user, text, exc, usage, trace
            )
            data = _extract_json(repaired)
            try:
                StrategyPolicy.model_validate(data)
            except ValidationError as second:
                raise PolicyInvalidError(
                    "Model output does not satisfy strategy-policy/1.0.",
                    details={
                        "error_count": len(second.errors()),
                        "first_error_count": len(exc.errors()),
                        "repaired": True,
                    },
                ) from second
            trace["repaired"] = True
        trace["turns"] = turns
        trace["latency_ms"] = latency_ms
        return PolicyGenerationResult(
            raw=data,
            model=self.model,
            latency_ms=latency_ms,
            usage=usage,
            simulated=False,
            source=self.provider,
            turns=turns,
            trace=trace,
        )

    # -- agent loop ------------------------------------------------------
    async def _openai_agent(
        self, system: str, user: str, toolbox: AgentToolbox
    ) -> tuple[str, dict[str, Any], dict[str, Any], int]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                },
            }
            for spec in toolbox.specs()
        ]
        # Response contract first, then the volatile context: every request in
        # the loop shares this prefix so the provider can serve it from cache.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": RESPONSE_CONTRACT},
            {"role": "user", "content": user},
        ]
        trace: dict[str, Any] = {"tools_available": True, "tool_calls": []}
        usage: dict[str, Any] = {}
        deadline = time.monotonic() + self.agent_budget_seconds
        spent = 0
        text = ""
        turns = 0

        for turn in range(1, self.max_turns + 1):
            turns = turn
            final_turn = (
                turn == self.max_turns
                or time.monotonic() >= deadline
                or spent >= MAX_TOOL_CONTEXT_CHARS
            )
            payload: dict[str, Any] = {
                "model": self.model,
                "temperature": 0.2,
                "messages": messages,
            }
            if final_turn:
                # One writing turn with no further investigation. Dropping the
                # tools field is accepted by every OpenAI-compatible server;
                # some reject ``tool_choice: "none"``.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "No further tool calls are available. Return the "
                            "strategy-policy JSON object now."
                        ),
                    }
                )
            else:
                payload["tools"] = tools
            data = await self._post(self._endpoint("/v1/chat/completions"), payload)
            _merge_usage(usage, data.get("usage"))
            choices = data.get("choices") or []
            if not choices:
                raise DependencyBadResponseError("Model response contained no choices.")
            message = choices[0].get("message") or {}
            tool_calls = message.get("tool_calls") or []
            if tool_calls and not final_turn:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": tool_calls,
                    }
                )
                # Several reads in one turn are the common case (both
                # symbols, market plus account). Run the independent ones
                # together, then write the answers back in request order so
                # the transcript stays deterministic.
                parallel_ok = spent < MAX_TOOL_CONTEXT_CHARS and all(
                    _runs_in_parallel(
                        toolbox, str((call.get("function") or {}).get("name") or "")
                    )
                    for call in tool_calls
                )
                if parallel_ok:
                    await asyncio.gather(
                        *(
                            self._call_tool(
                                toolbox,
                                str((call.get("function") or {}).get("name") or ""),
                                _parse_arguments(
                                    ((call.get("function") or {}).get("arguments"))
                                ),
                                parallel=True,
                            )
                            for call in tool_calls
                        )
                    )
                for call in tool_calls:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    arguments = _parse_arguments(function.get("arguments"))
                    if spent >= MAX_TOOL_CONTEXT_CHARS:
                        result, ok, latency_ms = (
                            {
                                "summary": "tool context budget exhausted",
                                "data": {"error": "tool context budget exhausted"},
                            },
                            False,
                            0,
                        )
                    else:
                        result, ok, latency_ms = await self._call_tool(
                            toolbox, name, arguments
                        )
                    content = _tool_content(result)
                    spent += len(content)
                    trace["tool_calls"].append(
                        {
                            "turn": turn,
                            "name": name,
                            "arguments": arguments,
                            "ok": ok,
                            "summary": result.get("summary", ""),
                            "latency_ms": latency_ms,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id") or "",
                            "content": content,
                        }
                    )
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else ""
            break

        if not text.strip():
            # Reasoning models sometimes spend the whole first pass thinking and
            # return no answer. Demand the JSON directly before giving up.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Return the strategy-policy JSON object now. "
                        "JSON only, no reasoning, no prose."
                    ),
                }
            )
            data = await self._post(self._endpoint("/v1/chat/completions"), payload)
            _merge_usage(usage, data.get("usage"))
            choices = data.get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            content = message.get("content")
            text = content if isinstance(content, str) else ""
            trace["forced_answer"] = True
        if not text.strip():
            raise PolicyInvalidError("Model returned no policy after the agent loop.")
        return text, usage, trace, turns

    async def _anthropic_agent(
        self, system: str, user: str, toolbox: AgentToolbox
    ) -> tuple[str, dict[str, Any], dict[str, Any], int]:
        tools = [
            {
                "name": spec["name"],
                "description": spec["description"],
                "input_schema": spec["parameters"],
            }
            for spec in toolbox.specs()
        ]
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": RESPONSE_CONTRACT},
            {"role": "user", "content": user},
        ]
        trace: dict[str, Any] = {"tools_available": True, "tool_calls": []}
        usage: dict[str, Any] = {}
        deadline = time.monotonic() + self.agent_budget_seconds
        spent = 0
        text = ""
        turns = 0

        for turn in range(1, self.max_turns + 1):
            turns = turn
            final_turn = (
                turn == self.max_turns
                or time.monotonic() >= deadline
                or spent >= MAX_TOOL_CONTEXT_CHARS
            )
            payload: dict[str, Any] = {
                "model": self.model,
                "max_tokens": 4096,
                "temperature": 0.2,
                "system": system,
                "messages": messages,
            }
            if final_turn:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "No further tool calls are available. Return the "
                            "strategy-policy JSON object now."
                        ),
                    }
                )
            else:
                payload["tools"] = tools
            data = await self._post(self._endpoint("/v1/messages"), payload)
            _merge_usage(usage, data.get("usage"))
            blocks = [
                block for block in (data.get("content") or []) if isinstance(block, dict)
            ]
            tool_uses = [block for block in blocks if block.get("type") == "tool_use"]
            if tool_uses and not final_turn:
                messages.append({"role": "assistant", "content": blocks})
                results: list[dict[str, Any]] = []
                if all(
                    _runs_in_parallel(toolbox, str(block.get("name") or ""))
                    for block in tool_uses
                ):
                    await asyncio.gather(
                        *(
                            self._call_tool(
                                toolbox,
                                str(block.get("name") or ""),
                                (
                                    block.get("input")
                                    if isinstance(block.get("input"), dict)
                                    else {}
                                ),
                                parallel=True,
                            )
                            for block in tool_uses
                        )
                    )
                for block in tool_uses:
                    name = str(block.get("name") or "")
                    arguments = (
                        block.get("input")
                        if isinstance(block.get("input"), dict)
                        else {}
                    )
                    if spent >= MAX_TOOL_CONTEXT_CHARS:
                        result, ok, latency_ms = (
                            {
                                "summary": "tool context budget exhausted",
                                "data": {"error": "tool context budget exhausted"},
                            },
                            False,
                            0,
                        )
                    else:
                        result, ok, latency_ms = await self._call_tool(
                            toolbox, name, arguments
                        )
                    content = _tool_content(result)
                    spent += len(content)
                    trace["tool_calls"].append(
                        {
                            "turn": turn,
                            "name": name,
                            "arguments": arguments,
                            "ok": ok,
                            "summary": result.get("summary", ""),
                            "latency_ms": latency_ms,
                        }
                    )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.get("id") or "",
                            "content": content,
                            "is_error": not ok,
                        }
                    )
                messages.append({"role": "user", "content": results})
                continue
            text = "".join(
                block.get("text", "")
                for block in blocks
                if block.get("type") == "text"
            )
            break

        if not text.strip():
            raise PolicyInvalidError("Model returned no policy after the agent loop.")
        return text, usage, trace, turns

    async def _repair_policy(
        self,
        user: str,
        text: str,
        error: ValidationError,
        usage: dict[str, Any],
        trace: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Ask the model to fix only what the validator rejected."""

        problems = [
            f"{'.'.join(str(part) for part in item['loc']) or 'root'}: {item['msg']}"
            for item in error.errors()[:12]
        ]
        if self.protocol == "anthropic":
            messages = [
                {"role": "user", "content": RESPONSE_CONTRACT},
                {"role": "user", "content": user},
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "That policy failed server validation:\n"
                        + "\n".join(f"- {problem}" for problem in problems)
                        + "\nReturn the corrected JSON object only."
                    ),
                },
            ]
            payload = {
                "model": self.model,
                "max_tokens": 4096,
                "temperature": 0.2,
                "system": RESPONSE_CONTRACT,
                "messages": messages[1:],
            }
            data = await self._post(self._endpoint("/v1/messages"), payload)
            blocks = data.get("content") or []
            repaired = "".join(
                block.get("text", "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            messages = [
                {"role": "system", "content": RESPONSE_CONTRACT},
                {"role": "user", "content": user},
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "That policy failed server validation:\n"
                        + "\n".join(f"- {problem}" for problem in problems)
                        + "\nReturn the corrected JSON object only."
                    ),
                },
            ]
            data = await self._post(
                self._endpoint("/v1/chat/completions"),
                {"model": self.model, "temperature": 0.2, "messages": messages},
            )
            choices = data.get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            content = message.get("content")
            repaired = content if isinstance(content, str) else ""
        _merge_usage(usage, data.get("usage"))
        trace["repair_attempt"] = True
        trace["repair_problems"] = problems
        return repaired, usage

    async def _call_tool(
        self,
        toolbox: AgentToolbox,
        name: str,
        arguments: dict[str, Any],
        *,
        parallel: bool = False,
    ) -> tuple[dict[str, Any], bool, int]:
        started = time.perf_counter()
        try:
            if parallel:
                result = await toolbox.call_parallel(name, arguments)
            else:
                result = await toolbox.call(name, arguments)
            ok = True
        except Exception as exc:  # noqa: BLE001 - tell the model, keep the loop alive
            result = {
                "summary": f"{type(exc).__name__}: {exc}",
                "data": {"error": str(exc), "tool": name},
            }
            ok = False
        return result, ok, int((time.perf_counter() - started) * 1000)

    async def list_models(self) -> list[str]:
        headers = self._headers()
        try:
            response = await self._client.get(self._endpoint("/v1/models"), headers=headers)
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError("Model discovery failed.") from exc
        if response.status_code >= 400:
            raise DependencyBadResponseError(
                "Model discovery was rejected.",
                details={"http_status": response.status_code},
            )
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise DependencyBadResponseError("Model discovery response was malformed.")
        return [
            str(item.get("id"))
            for item in data
            if isinstance(item, dict) and item.get("id")
        ]

    async def health(self) -> dict[str, object]:
        try:
            models = await self.list_models()
            return {
                "healthy": True,
                "provider": self.provider,
                "model": self.model,
                "models_visible": len(models),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "healthy": False,
                "provider": self.provider,
                "error": type(exc).__name__,
            }

    async def close(self) -> None:
        await self._client.aclose()


class OpenCodeGoAdapter(ConsoleLLMAdapter):
    """OpenCode Go: coding-agent traffic only.

    ``trade_runtime`` and ``backtest_analysis`` are denied server-side, not
    merely hidden in the UI.  Enabling them requires an explicit OpenCode
    confirmation that the workload is acceptable.
    """

    provider = "opencode_go"
    allowed_purposes = {"coding_agent", "strategy_authoring"}


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise PolicyInvalidError("Model output did not contain a JSON object.")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise PolicyInvalidError("Model output was not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise PolicyInvalidError("Model output JSON was not an object.")
    return parsed


def _runs_in_parallel(toolbox: AgentToolbox, name: str) -> bool:
    """Whether ``name`` may run alongside its siblings in the same turn.

    Toolboxes that do not advertise ``supports_parallel`` (test doubles,
    older adapters) are treated as strictly sequential.
    """

    check = getattr(toolbox, "supports_parallel", None)
    return bool(callable(check) and check(name))


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_content(result: dict[str, Any]) -> str:
    summary = result.get("summary")
    payload = json.dumps(
        result.get("data", {}), ensure_ascii=False, default=str, separators=(",", ":")
    )
    text = f"{summary}\n{payload}" if summary else payload
    return (
        text
        if len(text) <= TOOL_RESULT_CHARS
        else text[:TOOL_RESULT_CHARS] + "...[truncated]"
    )


def _merge_usage(total: dict[str, Any], usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
        else:
            total.setdefault(key, value)


def _retry_after_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(max(0.0, float(value)) * 1000)
    except ValueError:
        return None
