"""Audit service: redact, persist, hash-link and verify."""

from __future__ import annotations

from typing import Any

from ..storage.repository import Repository

SENSITIVE_KEY_PARTS = (
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "password",
    "token",
    "private_key",
    "passphrase",
)


def redact(value: Any) -> Any:
    """Recursively redact credential-shaped fields. Never mutates input."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


class AuditService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    async def record(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str | None,
        payload: dict[str, Any] | None = None,
    ):
        return await self.repo.append_audit(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=redact(payload or {}),
        )

    async def verify(self) -> dict[str, Any]:
        return await self.repo.verify_audit_chain()
