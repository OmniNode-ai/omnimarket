# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node entry point for the capsule-store projection (OMN-12842)."""

from __future__ import annotations

from omnimarket.nodes.node_projection_capsule_store.handlers.handler_capsule_store_projection import (
    HandlerCapsuleStoreProjection,
)


class NodeProjectionCapsuleStore(HandlerCapsuleStoreProjection):
    """ONEX entry-point wrapper for HandlerCapsuleStoreProjection."""


__all__ = ["NodeProjectionCapsuleStore"]
