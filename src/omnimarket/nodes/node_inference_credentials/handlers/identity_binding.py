# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The DI seam through which the hosted edge binds the caller-identity resolver.

The runtime builds the mount with no arguments, so the resolver is bound here once
by whoever composes the runtime. Until one is bound the mount refuses every request.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.edge_overlay_identity import overlay_identity_resolver
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

OVERLAY_ID = "node_inference_credentials"


class TenantIdentityResolverNotBoundError(RuntimeError):
    """No caller-identity resolver is bound for the mount."""


_BOUND: ProtocolHttpTenantIdentity | None = None


def bind_tenant_identity_resolver(resolver: ProtocolHttpTenantIdentity | None) -> None:
    """Bind (or with None, unbind) the resolver every request of the mount consults."""
    global _BOUND
    _BOUND = resolver


class LateBoundTenantIdentity:
    """Looks the bound resolver up per request, so binding order does not matter."""

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        if _BOUND is not None:
            return await _BOUND.resolve(headers)
        resolver = overlay_identity_resolver(OVERLAY_ID)
        if resolver is None:
            raise TenantIdentityResolverNotBoundError(
                "no tenant identity resolver is bound"
            )
        return await resolver.resolve(headers)


__all__: list[str] = [
    "OVERLAY_ID",
    "LateBoundTenantIdentity",
    "TenantIdentityResolverNotBoundError",
    "bind_tenant_identity_resolver",
]
