# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The DI seam through which the hosted edge binds the caller-identity resolver.

The runtime builds the mount with no arguments, so the resolver is bound here, either
explicitly by whoever composes the runtime or, when none is bound, from the node's
deployment overlay (``node_inference_credentials/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``). With neither the mount refuses every request.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_inference_credentials.handlers.edge_identity import (
    BearerJwtTenantIdentity,
    JwksSigningKeyProvider,
)
from omnimarket.nodes.node_inference_credentials.models.model_edge_identity_overlay import (
    ModelEdgeIdentityOverlay,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

OVERLAY_ID = "node_inference_credentials"


class TenantIdentityResolverNotBoundError(RuntimeError):
    """No caller-identity resolver is bound for the mount."""


_BOUND: ProtocolHttpTenantIdentity | None = None
_FROM_OVERLAY: ProtocolHttpTenantIdentity | None = None


def bind_tenant_identity_resolver(resolver: ProtocolHttpTenantIdentity | None) -> None:
    """Bind (or with None, unbind) the resolver every request of the mount consults."""
    global _BOUND, _FROM_OVERLAY
    _BOUND = resolver
    _FROM_OVERLAY = None


def _resolver_from_overlay() -> ProtocolHttpTenantIdentity:
    """Build (once) the edge resolver the deployment overlay describes."""
    global _FROM_OVERLAY
    if _FROM_OVERLAY is not None:
        return _FROM_OVERLAY
    try:
        overlay = load_structured_overlay(OVERLAY_ID, ModelEdgeIdentityOverlay)
    except NodeOverlayNotFoundError as exc:
        raise TenantIdentityResolverNotBoundError(str(exc)) from None
    _FROM_OVERLAY = BearerJwtTenantIdentity(overlay, JwksSigningKeyProvider(overlay))
    return _FROM_OVERLAY


class LateBoundTenantIdentity:
    """Looks the bound resolver up per request, so binding order does not matter."""

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        resolver = _BOUND if _BOUND is not None else _resolver_from_overlay()
        return await resolver.resolve(headers)


__all__: list[str] = [
    "OVERLAY_ID",
    "LateBoundTenantIdentity",
    "TenantIdentityResolverNotBoundError",
    "bind_tenant_identity_resolver",
]
