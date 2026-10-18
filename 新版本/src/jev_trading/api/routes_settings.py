"""Operator settings the UI is allowed to change at runtime.

Two concerns live here:

* **Model connections.** An API key is write-only: the client may send one,
  the server persists it in ``system_state`` and rebuilds the adapter, and no
  endpoint ever returns it. Reads expose only *configured* booleans, the base
  URL and the model name.
* **Auto-cycle.** The background cadence can be switched on or off without a
  container restart. Existing positions keep their deterministic exits either
  way; disabling only stops new cycles.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .security import require_operator

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


def _container(request: Request):
    return request.app.state.container


class ProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    base_url: str | None = Field(default=None, max_length=255)
    # Write-only. Omitted or empty keeps the stored secret; clear_api_key wipes it.
    api_key: str | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    model: str | None = Field(default=None, max_length=128)

    @field_validator("base_url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL")
        return value.rstrip("/")


class LLMInput(ProviderInput):
    protocol: Literal["openai", "anthropic"] | None = None


class JevInput(ProviderInput):
    path: str | None = Field(default=None, max_length=128)


class ModelSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMInput | None = None
    jev: JevInput | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> ModelSettingsRequest:
        if self.llm is None and self.jev is None:
            raise ValueError("provide llm, jev, or both")
        return self


class AutomationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    interval_seconds: int = Field(default=300, ge=10, le=3600)


@router.get("/models")
async def get_model_settings(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    return _container(request).model_settings()


@router.put("/models")
async def update_model_settings(
    request: Request,
    payload: ModelSettingsRequest,
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    return await _container(request).update_model_settings(
        llm=payload.llm.model_dump(exclude_unset=True) if payload.llm else None,
        jev=payload.jev.model_dump(exclude_unset=True) if payload.jev else None,
        actor=actor,
    )


@router.get("/automation")
async def get_automation(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, object]:
    return _container(request).automation_status()


@router.put("/automation")
async def update_automation(
    request: Request,
    payload: AutomationRequest,
    actor: str = Depends(require_operator),
) -> dict[str, object]:
    return await _container(request).set_automation(
        enabled=payload.enabled,
        interval_seconds=payload.interval_seconds,
        actor=actor,
    )
