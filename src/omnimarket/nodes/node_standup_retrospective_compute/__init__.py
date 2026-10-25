# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local ONEX wrapper for node_standup_retrospective_compute."""

from omnimarket.nodes.node_standup_retrospective_compute.handlers.handler_standup_retrospective import (
    HandlerStandupRetrospective,
)


class NodeStandupRetrospectiveCompute(HandlerStandupRetrospective):
    """ONEX entry-point wrapper for HandlerStandupRetrospective."""


__all__ = ["HandlerStandupRetrospective", "NodeStandupRetrospectiveCompute"]
