# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A signed-in customer's key registration, sent to the gateway over HTTPS (OMN-26731).

``onex secret register-tenant-key`` used to publish on the message service, which
a customer does not have. When the CLI is signed in to a gateway (the stored
dashboard API key ``onex cloud login`` writes) the key and the chosen models go
to the gateway's ``POST /v1/workflows/tenant-keys``, which writes them under the
tenant UUID the sign-in resolves to. This module is the client side of that one
call; the transport is injected so a fake gateway and the dev gateway run the
same command.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_infra.gateway.client.store_gateway_credential import (
    StoreGatewayCredential,
)
from omnibase_infra.gateway.models.model_gateway_api_key import (
    ModelGatewayApiKeyCredential,
)
from pydantic import SecretStr

TENANT_KEYS_PATH = "/v1/workflows/tenant-keys"
_API_KEY_HEADER = "x-api-key"
_TIMEOUT_SECONDS = 60.0


class GatewayKeyRegistrationError(Exception):
    """The gateway refused or could not take the key. Carries no value."""


def load_signed_in_gateway(
    onex_home: Path | None = None,
) -> tuple[str, SecretStr] | None:
    """The stored gateway origin and API key, or ``None`` when not signed in."""
    store = StoreGatewayCredential(onex_home=onex_home or Path.home() / ".onex")
    try:
        credential = store.load()
    except ModelOnexError:
        return None
    if not isinstance(credential, ModelGatewayApiKeyCredential):
        return None
    return credential.base_url, credential.api_key


def register_through_gateway(
    *,
    base_url: str,
    api_key: SecretStr,
    value: str,
    provider: str,
    name: str,
    models: list[str] | None,
    tenant: str | None = None,
    plan: str | None = None,
    expected_version: int | None = None,
    expires_at: datetime | None = None,
    workspace_id: str | None = None,
    base_url_override: str | None = None,
    connection_id: str | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Send the key and models to the gateway and return its non-secret view."""
    payload: dict[str, Any] = {
        "provider": provider,
        "name": name,
        "key_value": value,
        "models": models,
        "tenant": tenant,
        "plan": plan,
        "expected_version": expected_version,
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
        "workspace_id": workspace_id,
        "base_url": base_url_override,
        "connection_id": connection_id,
    }
    body = {k: v for k, v in payload.items() if v is not None}
    owns_client = http_client is None
    # The customer's CLI calling the gateway over HTTPS has no bus-mediated
    # transport: this is the one call that opens the socket, injectable for tests.
    client = http_client or httpx.Client(  # no-contract-check: the seam
        timeout=_TIMEOUT_SECONDS
    )
    try:
        response = client.post(
            f"{base_url.rstrip('/')}{TENANT_KEYS_PATH}",
            json=body,
            headers={_API_KEY_HEADER: api_key.get_secret_value()},
        )
    except httpx.HTTPError as exc:
        raise GatewayKeyRegistrationError(
            f"the gateway could not be reached ({type(exc).__name__}); "
            "nothing was stored."
        ) from exc
    finally:
        if owns_client:
            client.close()
    if response.status_code == 201:
        view = response.json()
        if isinstance(view, dict):
            return view
        raise GatewayKeyRegistrationError("the gateway answered an unreadable view")
    raise GatewayKeyRegistrationError(_refusal_text(response))


def _refusal_text(response: httpx.Response) -> str:
    detail: object = None
    try:
        decoded = response.json()
        detail = decoded.get("detail") if isinstance(decoded, dict) else None
    except ValueError:
        detail = None
    text = detail if isinstance(detail, str) else f"HTTP {response.status_code}"
    if response.status_code == 401:
        text = "the gateway did not accept this sign-in; run 'onex cloud login' again"
    return f"the gateway refused the key: {text}. Nothing was stored."


__all__: list[str] = [
    "TENANT_KEYS_PATH",
    "GatewayKeyRegistrationError",
    "load_signed_in_gateway",
    "register_through_gateway",
]
