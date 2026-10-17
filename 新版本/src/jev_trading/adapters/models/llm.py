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
from .base import PolicyContext, PolicyGenerationResult

SYSTEM_PROMPT = """You are the strategy layer of an automated trading system.
Return exactly one JSON object that validates against schema strategy-policy/1.0.

Rules:
- Output JSON only. No prose, no markdown, no code fences.
- Never propose limits above absolute_server_limits; they are hard caps.
- mode_scope may only contain modes you were told are active.
- Keep rationale under 400 words.
- You never place orders and never see exchange credentials."""

ALLOWED_PURPOSES = {
    "coding_agent",
    "strategy_authoring",
    "trade_runtime",
    "backtest_analysis",
}


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
        self._client = httpx.AsyncClient(timeout=timeout)

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
        self, context: PolicyContext, *, purpose: str | None = None
    ) -> PolicyGenerationResult:
        effective_purpose = purpose or self.purpose
        self.ensure_purpose(effective_purpose)
        started = time.perf_counter()
        text, usage = await self._chat(
            SYSTEM_PROMPT,
            json.dumps(context.model_dump(mode="json"), ensure_ascii=False),
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        data = _extract_json(text)
        try:
            StrategyPolicy.model_validate(data)
        except ValidationError as exc:
            raise PolicyInvalidError(
                "Model output does not satisfy strategy-policy/1.0.",
                details={"error_count": len(exc.errors())},
            ) from exc
        return PolicyGenerationResult(
            raw=data,
            model=self.model,
            latency_ms=latency_ms,
            usage=usage,
            simulated=False,
            source=self.provider,
        )

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


def _retry_after_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(max(0.0, float(value)) * 1000)
    except ValueError:
        return None
