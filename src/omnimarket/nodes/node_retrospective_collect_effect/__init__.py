# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_retrospective_collect_effect: scaffold from the weekly_retrospective design."""

from omnimarket.nodes.node_retrospective_collect_effect.handlers.handler_retrospective_collect_effect import (
    HandlerRetrospectiveCollectEffect,
)

__all__ = ["HandlerRetrospectiveCollectEffect", "NodeRetrospectiveCollectEffect"]


class NodeRetrospectiveCollectEffect(HandlerRetrospectiveCollectEffect):
    """ONEX entry-point wrapper for HandlerRetrospectiveCollectEffect."""
