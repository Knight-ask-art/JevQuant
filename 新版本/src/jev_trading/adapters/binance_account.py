"""Binance account/permission preflight (signed REST, Windows-safe)."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from ..domain.errors import (
    DependencyBadResponseError,
    DependencyUnavailableError,
    ProviderRateLimitedError,
)

CRITICAL_PERMISSIONS = ("enableReading", "enableSpotAndMarginTrading")


class BinanceAccountClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        api_secret: str | None,
        timeout: float = 15.0,
        recv_window: int = 5000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self._api_secret = api_secret or ""
        self.recv_window = recv_window
        self._client = httpx.AsyncClient(timeout=timeout)
        self.configured = bool(self._api_key and self._api_secret)

    def _signed_query(self, params: dict[str, Any]) -> str:
        payload = dict(params)
        payload["timestamp"] = int(time.time() * 1000)
        payload["recvWindow"] = self.recv_window
        query = urlencode(payload)
        signature = hmac.new(
            self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise DependencyUnavailableError("Binance API credentials are not configured.")
        url = f"{self.base_url}{path}?{self._signed_query(params or {})}"
        try:
            response = await self._client.get(url, headers={"X-MBX-APIKEY": self._api_key})
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(
                "Binance account request failed.", details={"reason": type(exc).__name__}
            ) from exc
        if response.status_code == 429:
            raise ProviderRateLimitedError("Binance rate limit reached.")
        if response.status_code == 418:
            raise DependencyUnavailableError("Binance returned HTTP 418.")
        if response.status_code == 451:
            raise DependencyUnavailableError("Binance returned HTTP 451 (restricted location).")
        if response.status_code >= 400:
            raise DependencyBadResponseError(
                "Binance rejected the signed request.",
                details={"http_status": response.status_code},
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise DependencyBadResponseError("Binance returned non-JSON data.") from exc
        if not isinstance(data, dict):
            raise DependencyBadResponseError("Binance response was not an object.")
        return data

    async def permissions(self) -> dict[str, Any]:
        return await self._get("/sapi/v1/account/apiRestrictions")

    async def account(self) -> dict[str, Any]:
        return await self._get("/api/v3/account")

    async def preflight(
        self,
        *,
        require_ip_restrict: bool = True,
        require_withdrawals_disabled: bool = True,
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        try:
            permissions = await self.permissions()
        except Exception as exc:  # noqa: BLE001
            return {
                "passed": False,
                "checks": [
                    {
                        "name": "permission_endpoint",
                        "passed": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                ],
            }

        for key in CRITICAL_PERMISSIONS:
            checks.append(
                {
                    "name": key,
                    "passed": bool(permissions.get(key)),
                    "detail": f"{key}={permissions.get(key)}",
                }
            )
        withdrawals = bool(permissions.get("enableWithdrawals"))
        checks.append(
            {
                "name": "withdrawals_disabled",
                "passed": (not withdrawals) if require_withdrawals_disabled else True,
                "detail": f"enableWithdrawals={withdrawals}",
            }
        )
        ip_restrict = bool(permissions.get("ipRestrict"))
        checks.append(
            {
                "name": "ip_restrict",
                "passed": ip_restrict if require_ip_restrict else True,
                "detail": f"ipRestrict={ip_restrict}",
            }
        )
        return {"passed": all(check["passed"] for check in checks), "checks": checks}

    async def close(self) -> None:
        await self._client.aclose()
