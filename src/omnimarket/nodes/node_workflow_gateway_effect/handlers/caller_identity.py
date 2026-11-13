# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller identity for the workflow gateway mount: bearer token or tenant API key.

A bearer credential goes to the bearer resolver only; a failed bearer is never
retried as an API key, so a misconfigured issuer reads as a refusal and not as a
silent downgrade.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

_PRINCIPAL_PREFIX = "t-"


def derive_principal_id(tenant_id: UUID) -> str:
    """The immutable principal of a tenant: ``t-`` and the 32 hex digits of its UUID.

    Derived from the UUID alone, never from the slug, so a slug rename cannot change
    any authorization outcome.
    """
    return f"{_PRINCIPAL_PREFIX}{tenant_id.hex}"


def header_value(headers: Mapping[str, str], name: str) -> str:
    """The value of a header by case-insensitive name, or an empty string."""
    wanted = name.lower()
    return next((v for k, v in headers.items() if k.lower() == wanted), "")


class CompositeCallerIdentity:
    """Resolve the caller from a bearer token or, without one, from an API key."""

    def __init__(
        self,
        *,
        bearer: ProtocolHttpTenantIdentity | None,
        api_key: ProtocolHttpTenantIdentity | None,
        api_key_header: str,
    ) -> None:
        self._bearer = bearer
        self._api_key = api_key
        self._api_key_header = api_key_header

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        if header_value(headers, "authorization").lower().startswith("bearer "):
            return None if self._bearer is None else await self._bearer.resolve(headers)
        if header_value(headers, self._api_key_header):
            return (
                None if self._api_key is None else await self._api_key.resolve(headers)
            )
        return None


__all__: list[str] = ["CompositeCallerIdentity", "derive_principal_id", "header_value"]
