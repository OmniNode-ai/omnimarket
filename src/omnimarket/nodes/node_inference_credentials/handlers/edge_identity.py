# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime edge's caller-identity resolver: a verified bearer token names the tenant.

Implements ``ProtocolHttpTenantIdentity``. A request with no bearer token, a token that
fails signature, issuer, audience or expiry checks, or a token that lacks the tenant
claims resolves to ``None`` (the mount answers 401). An unreadable key set raises, and
the handler answers 503: the edge never serves a caller it could not judge.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import jwt
from pydantic import ValidationError

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_inference_credentials.models.model_edge_identity_overlay import (
    ModelEdgeIdentityOverlay,
)
from omnimarket.nodes.node_inference_credentials.protocols.protocol_signing_key_provider import (
    ProtocolSigningKeyProvider,
    SigningKeyUnavailableError,
)


class JwksSigningKeyProvider:
    """Reads the issuer's published key set, cached for the overlay's lifespan."""

    def __init__(self, overlay: ModelEdgeIdentityOverlay) -> None:
        self._client = jwt.PyJWKClient(
            overlay.jwks_url, cache_keys=True, lifespan=overlay.jwks_cache_seconds
        )

    def signing_key_for(self, token: str) -> Any:
        try:
            return self._client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as exc:
            raise SigningKeyUnavailableError(type(exc).__name__) from None


class BearerJwtTenantIdentity:
    """Resolve the caller tenant from ``Authorization: Bearer <jwt>``."""

    def __init__(
        self, overlay: ModelEdgeIdentityOverlay, keys: ProtocolSigningKeyProvider
    ) -> None:
        self._overlay = overlay
        self._keys = keys

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        authorization = next(
            (v for k, v in headers.items() if k.lower() == "authorization"), ""
        )
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return None
        try:
            key = await asyncio.to_thread(self._keys.signing_key_for, token)
            claims = jwt.decode(
                token,
                key,
                algorithms=list(self._overlay.algorithms),
                audience=self._overlay.audience,
                issuer=self._overlay.issuer,
                leeway=self._overlay.leeway_seconds,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError:
            return None
        try:
            return ModelHttpTenantIdentity(
                tenant_id=UUID(str(claims[self._overlay.tenant_id_claim])),
                tenant_slug=claims[self._overlay.tenant_slug_claim],
                principal_id=claims[self._overlay.principal_id_claim],
            )
        except (KeyError, ValueError, TypeError, ValidationError):
            return None


__all__: list[str] = ["BearerJwtTenantIdentity", "JwksSigningKeyProvider"]
