# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Edge refusals of the BYOK credential mount, mirroring the hosted API's bodies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

CODE_CREDENTIAL_ABSENT = "auth.credential.absent"
CODE_CREDENTIAL_INVALID = "auth.credential.invalid"
CODE_RESOURCE_NOT_FOUND = "resource.not_found"

_API_KEY_HEADER = "x-api-key"
_AUTHORIZATION_HEADER = "authorization"


def not_configured_refusal() -> tuple[int, dict[str, Any]]:
    """No resolver bound: answers as the hosted API with its OIDC uninitialised."""
    return 503, {"detail": "OIDC authentication is not configured"}


def no_tenant_refusal(headers: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
    """A resolver was bound and found no tenant for the request."""
    authorization = headers.get(_AUTHORIZATION_HEADER, "")
    if authorization.lower().startswith("bearer "):
        return 401, {"detail": "tenant_not_provisioned"}
    presented = bool(headers.get(_API_KEY_HEADER)) or bool(authorization)
    code = CODE_CREDENTIAL_INVALID if presented else CODE_CREDENTIAL_ABSENT
    return 401, {"detail": "Unauthorized", "code": code}


async def resolve_caller(
    resolver: ProtocolHttpTenantIdentity | None, headers: Mapping[str, str]
) -> ModelHttpTenantIdentity | tuple[int, dict[str, Any]]:
    """The caller tenant, or the refusal to answer with. Never a default tenant."""
    if resolver is None:
        return not_configured_refusal()
    try:
        identity = await resolver.resolve(headers)
    except Exception:
        return 500, {"detail": "Internal Server Error"}
    if identity is None:
        return no_tenant_refusal(headers)
    return identity
