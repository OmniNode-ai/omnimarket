# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Infisical's REST API as a ``ProtocolSecretStore`` (OMN-20944).

The vendor-facing half of node_secret_store_effect, and the only file in the
node that knows the Infisical wire format (the vendor boundary doctrine,
docs/architecture/ONEX_CANONICAL_ARCHITECTURE.md "Vendor boundary"). It is
invoked only by the node's own handler, through the omnibase_spi
``ProtocolSecretStore`` seam. It opens no connection itself: the node's routed
handler owns the contract-declared HTTP transport and hands it an exchange.

Keys are absolute paths, ``<folder>/<name>``: ``/captured/CAPTURED_ab12`` is
the secret ``CAPTURED_ab12`` in folder ``/captured``. ``list_keys(prefix)``
takes a folder and returns its keys in the same form.

Write posture: CREATE ONLY. ``set_secret`` creates a key and returns ``False``
for a key that already exists, without reading or updating it, so a
create-only machine identity is enough to write. ``delete_secret`` is not
offered and raises ``RuntimeError``, which the protocol allows for a store
whose write paths are restricted.

Errors carry an HTTP status or an exception type name, never a response body:
a store error can echo the request, and a create request carries the value.
``PermissionError`` is a refusal (401/403), ``ConnectionError`` is a store that
did not answer or answered 429/5xx, and ``ValueError`` is any other rejection.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any, NoReturn

from pydantic import SecretStr

from omnimarket.nodes.node_secret_store_effect.adapters.http_exchange import (
    HttpExchange,
)

logger = logging.getLogger(__name__)

PROVIDER = "infisical"

#: Re-login this long before the token's stated expiry.
_TOKEN_SLACK_SECONDS = 30.0


def split_key(key: str) -> tuple[str, str]:
    """``/a/b/NAME`` into (``/a/b``, ``NAME``); a bare ``NAME`` is in ``/``.

    Raises:
        ValueError: an empty name or a name with surrounding whitespace.
    """
    folder, _, name = key.rpartition("/")
    if not name or name != name.strip():
        raise ValueError("a secret key needs a non-empty name with no padding")
    return (folder or "/"), name


def _raise_for_status(status: int, operation: str) -> NoReturn:
    if status in (401, 403):
        raise PermissionError(f"{operation} refused with HTTP {status}")
    if status == 429 or status >= 500:
        raise ConnectionError(f"{operation} unavailable with HTTP {status}")
    raise ValueError(f"{operation} rejected with HTTP {status}")


class HandlerInfisicalHttp:
    """``ProtocolSecretStore`` over Infisical's universal-auth and raw secrets API.

    One instance is one machine identity in one project and environment. The
    access token is cached in memory and renewed on expiry or on one 401.
    """

    def __init__(
        self,
        *,
        address: str,
        project_id: str,
        environment: str,
        client_id: SecretStr,
        client_secret: SecretStr,
        exchange: HttpExchange,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._address = address.rstrip("/")
        self._project_id = project_id
        self._environment = environment
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout_seconds
        self._exchange = exchange
        self._token: SecretStr | None = None
        self._token_expires_at = 0.0

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
        token: SecretStr | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        try:
            return await self._exchange(
                method,
                f"{self._address}{path}",
                params=params,
                payload=payload,
                headers=headers,
                timeout=self._timeout,
            )
        except ConnectionError as exc:
            raise ConnectionError(f"{operation} failed: {exc}") from None

    async def _login(self) -> SecretStr:
        if self._token is not None and time.monotonic() < self._token_expires_at:
            return self._token
        status, body = await self._request(
            "POST",
            "/api/v1/auth/universal-auth/login",
            operation="login",
            payload={
                "clientId": self._client_id.get_secret_value(),
                "clientSecret": self._client_secret.get_secret_value(),
            },
        )
        token = body.get("accessToken") if status == 200 else None
        if not isinstance(token, str) or not token:
            _raise_for_status(status if status != 200 else 502, "login")
        expires_in = body.get("expiresIn")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else 300.0
        self._token = SecretStr(str(token))
        self._token_expires_at = time.monotonic() + max(
            lifetime - _TOKEN_SLACK_SECONDS, 1.0
        )
        return self._token

    async def _authorized(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, str] | None = None,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """One call with the cached token, renewed once on a 401."""
        status, body = await self._request(
            method,
            path,
            operation=operation,
            params=params,
            payload=payload,
            token=await self._login(),
        )
        if status == 401:
            self._token = None
            status, body = await self._request(
                method,
                path,
                operation=operation,
                params=params,
                payload=payload,
                token=await self._login(),
            )
        return status, body

    def _scope(self, folder: str) -> dict[str, str]:
        return {
            "workspaceId": self._project_id,
            "environment": self._environment,
            "secretPath": folder,
        }

    async def get_secret(self, key: str) -> str | None:
        folder, name = split_key(key)
        status, body = await self._authorized(
            "GET",
            f"/api/v3/secrets/raw/{urllib.parse.quote(name, safe='')}",
            operation="read",
            params=self._scope(folder),
        )
        if status == 404:
            return None
        if status != 200:
            _raise_for_status(status, "read")
        secret = body.get("secret")
        value = secret.get("secretValue") if isinstance(secret, dict) else None
        if not isinstance(value, str):
            raise ValueError("read answered without a secret value")
        return value

    async def set_secret(self, key: str, value: str) -> bool:
        """Create ``key``; ``False`` when it already exists (create only)."""
        folder, name = split_key(key)
        status, body = await self._authorized(
            "POST",
            f"/api/v3/secrets/raw/{urllib.parse.quote(name, safe='')}",
            operation="create",
            payload={**self._scope(folder), "secretValue": value, "type": "shared"},
        )
        if status in (200, 201):
            return True
        message = str(body.get("message", "")).lower()
        # The existing-key case: 409 on current servers, 400 "already exist" on
        # older ones. Any other 400 is a rejection, not an existing secret.
        if status == 409 or (status == 400 and "already exist" in message):
            return False
        _raise_for_status(status, "create")

    async def delete_secret(self, key: str) -> bool:
        del key
        raise RuntimeError("this secret store is create-only; delete is not offered")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        folder = (prefix or "/").rstrip("/") or "/"
        status, body = await self._authorized(
            "GET", "/api/v3/secrets/raw", operation="list", params=self._scope(folder)
        )
        if status == 404:
            return []
        if status != 200:
            _raise_for_status(status, "list")
        entries = body.get("secrets")
        names = [
            entry["secretKey"]
            for entry in (entries if isinstance(entries, list) else [])
            if isinstance(entry, dict) and isinstance(entry.get("secretKey"), str)
        ]
        base = "" if folder == "/" else folder
        return sorted(f"{base}/{name}" for name in names)

    async def health_check(self) -> bool:
        try:
            await self._login()
        except (PermissionError, ConnectionError, ValueError):
            return False
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        self._token = None
        self._token_expires_at = 0.0


__all__ = ["PROVIDER", "HandlerInfisicalHttp", "split_key"]
