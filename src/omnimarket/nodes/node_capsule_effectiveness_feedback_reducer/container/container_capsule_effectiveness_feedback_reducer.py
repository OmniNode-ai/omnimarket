# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds node_capsule_effectiveness_feedback_reducer handlers."""

from omnimarket.nodes.node_projection_capsule_store.handlers.handler_capsule_store_projection import (
    HandlerCapsuleStoreProjection,
)


def build_handler_capsule_store_projection() -> HandlerCapsuleStoreProjection:
    """Build the handler with its constructor defaults."""
    return HandlerCapsuleStoreProjection()
