# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A shared, overlay-backed caller-identity resolver for runtime HTTP nodes.

The resolver is built from a node's own overlay document and cached on success; an
absent document is not cached so one that appears later is used without a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from omnimarket.handlers.edge_bearer_jwt_identity import (
    BearerJwtTenantIdentity,
    JwksSigningKeyProvider,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.model_edge_identity_overlay import ModelEdgeIdentityOverlay
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

logger = logging.getLogger(__name__)


class EdgeIdentityNotBoundError(RuntimeError):
    """No overlay document supplies the resolver."""


_RESOLVER_CACHE: dict[str, ProtocolHttpTenantIdentity] = {}


def overlay_identity_resolver(
    overlay_id: str,
) -> ProtocolHttpTenantIdentity | None:
    """Build the resolver from the node's overlay document, or ``None``.

    A missing document is not cached so a document that appears later is used
    without a restart; any other overlay error is logged once and not cached.
    """
    cached = _RESOLVER_CACHE.get(overlay_id)
    if cached is not None:
        return cached
    try:
        overlay = load_structured_overlay(overlay_id, ModelEdgeIdentityOverlay)
    except NodeOverlayNotFoundError:
        return None
    except NodeOverlayError as exc:
        logger.warning(
            "identity overlay %s unavailable (%s)",
            overlay_id,
            type(exc).__name__,
        )
        return None
    resolver = BearerJwtTenantIdentity(overlay, JwksSigningKeyProvider(overlay))
    _RESOLVER_CACHE[overlay_id] = resolver
    return resolver


def clear_overlay_identity_cache() -> None:
    """Drop every cached resolver so the next lookup re-reads the overlay."""
    _RESOLVER_CACHE.clear()


class OverlayBoundTenantIdentity:
    """Resolves the caller tenant from the node's own overlay document."""

    def __init__(self, overlay_id: str) -> None:
        self._overlay_id = overlay_id

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        resolver = overlay_identity_resolver(self._overlay_id)
        if resolver is None:
            raise EdgeIdentityNotBoundError(
                f"no overlay document supplies the identity resolver for {self._overlay_id}"
            )
        return await resolver.resolve(headers)


__all__: list[str] = [
    "EdgeIdentityNotBoundError",
    "OverlayBoundTenantIdentity",
    "clear_overlay_identity_cache",
    "overlay_identity_resolver",
]
