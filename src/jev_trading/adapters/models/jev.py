"""Jev System One adapter.

Jev answers bounded, typed questions (choice / score / noul) about a single
candidate snapshot.  It never receives exchange credentials and never
places orders.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any
from uuid import uuid4

import httpx

from ...domain.errors import (
    DependencyBadResponseError,
    DependencyUnavailableError,
    ProviderRateLimitedError,
)
from .base import ChoiceAnswer, JevResult, NoulAnswer, ScoreAnswer

VALID_CHOICES = {"buy", "wait", "reject"}


def _number(value: Any, field: str) -> float:
    """Parse a provider-supplied number without letting ValueError escape.

    A malformed Jev answer must fail closed for that signal, not abort the
    whole cycle (which would also skip exit management for open positions).
    """

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DependencyBadResponseError(f"Jev {field} was not a number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DependencyBadResponseError(f"Jev {field} was not a number.") from exc
    if not math.isfinite(number):
        raise DependencyBadResponseError(f"Jev {field} was not finite.")
    return number


class JevAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "jev-1.13",
        path: str = "/v1/decisions",
        timeout: float = 15.0,
        max_retries: int = 1,
    ) -> None:
        if not base_url or not api_key:
            raise DependencyUnavailableError("Jev is not fully configured.")
        self.base_url = base_url.rstrip("/")
        self.path = path if path.startswith("/") else f"/{path}"
        self._api_key = api_key
        self.model_name = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout)

    async def evaluate(
        self,
        state: dict[str, Any],
        questions: dict[str, Any],
        *,
        signal_id: str | None = None,
    ) -> JevResult:
        payload = {"model": self.model_name, "state": state, "questions": questions}
        started = time.perf_counter()
        data = await self._post(payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._normalize(data, latency_ms=latency_ms)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = await self._client.post(
                    self.base_url + self.path,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "content-type": "application/json",
                        "user-agent": "jev-trading/0.1",
                    },
                    json=payload,
                )
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise DependencyUnavailableError(
                        "Jev is unreachable.", details={"reason": type(exc).__name__}
                    ) from exc
            else:
                if response.status_code in (401, 403):
                    raise DependencyUnavailableError(
                        "Jev rejected the credential.",
                        details={"http_status": response.status_code},
                    )
                if response.status_code == 429:
                    if attempt >= self.max_retries:
                        raise ProviderRateLimitedError("Jev rate limit reached.")
                elif response.status_code >= 500:
                    if attempt >= self.max_retries:
                        raise DependencyUnavailableError(
                            "Jev returned a server error.",
                            details={"http_status": response.status_code},
                        )
                elif response.status_code >= 400:
                    raise DependencyBadResponseError(
                        "Jev rejected the evaluation request.",
                        details={"http_status": response.status_code},
                    )
                else:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise DependencyBadResponseError("Jev returned non-JSON data.") from exc
                    if not isinstance(data, dict):
                        raise DependencyBadResponseError("Jev response was not an object.")
                    return data
            attempt += 1
            await asyncio.sleep(min(2.0, 0.25 * (2**attempt)))

    def _normalize(self, data: dict[str, Any], *, latency_ms: int) -> JevResult:
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise DependencyBadResponseError("Jev response is missing answers.")
        entry = answers.get("entry_action")
        if not isinstance(entry, dict):
            raise DependencyBadResponseError("Jev response is missing entry_action.")
        choice = str(entry.get("choice", "")).lower()
        if choice not in VALID_CHOICES:
            raise DependencyBadResponseError("Jev entry_action.choice was invalid.")
        raw_probabilities = entry.get("probabilities") or {}
        if not isinstance(raw_probabilities, dict):
            raise DependencyBadResponseError("Jev entry_action.probabilities was invalid.")
        probabilities = {
            key: _number(value, f"probability[{key}]")
            for key, value in raw_probabilities.items()
            if key in VALID_CHOICES
        }
        for value in probabilities.values():
            if not 0.0 <= value <= 1.0:
                raise DependencyBadResponseError("Jev probabilities were out of range.")
        confidence = _number(
            entry.get("confidence", probabilities.get(choice, 0.0)), "confidence"
        )
        if not 0.0 <= confidence <= 1.0:
            raise DependencyBadResponseError("Jev confidence was out of range.")

        quality_answer = answers.get("entry_quality")
        if quality_answer is not None and not isinstance(quality_answer, dict):
            raise DependencyBadResponseError("Jev entry_quality was invalid.")
        quality = _number((quality_answer or {}).get("score", 0.0), "entry_quality.score")
        if quality < 0.0:
            raise DependencyBadResponseError("Jev entry_quality.score was negative.")
        fit_answer = answers.get("policy_fit")
        if fit_answer is not None and not isinstance(fit_answer, dict):
            raise DependencyBadResponseError("Jev policy_fit was invalid.")
        fit = _number((fit_answer or {}).get("noul", 0.0), "policy_fit.noul")
        if not 0.0 <= fit <= 1.0:
            raise DependencyBadResponseError("Jev policy_fit was out of range.")

        return JevResult(
            model=str(data.get("model") or self.model_name),
            evaluation_id=str(data.get("evaluation_id") or f"jev_{uuid4().hex[:16]}"),
            entry_action=ChoiceAnswer(
                choice=choice, confidence=confidence, probabilities=probabilities
            ),
            entry_quality=ScoreAnswer(score=quality),
            policy_fit=NoulAnswer(noul=fit),
            latency_ms=latency_ms,
            usage=dict(data.get("usage") or {}),
            simulated=False,
            raw=data,
        )

    async def health(self) -> dict[str, object]:
        try:
            response = await self._client.get(
                self.base_url + "/v1/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            return {
                "healthy": response.status_code < 500,
                "provider": "jev",
                "model": self.model_name,
                "http_status": response.status_code,
            }
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "provider": "jev", "error": type(exc).__name__}

    async def close(self) -> None:
        await self._client.aclose()
