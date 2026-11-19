# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared-blocker escalation compute node (OMN-25406)."""

from omnimarket.nodes.node_shared_blocker_escalation_compute.handlers.handler_shared_blocker_escalation import (
    HandlerAndonStartGuard,
    HandlerSharedBlockerEscalation,
)


class NodeSharedBlockerEscalationCompute(HandlerSharedBlockerEscalation):
    """ONEX entrypoint for the shared-blocker escalation judge."""


__all__ = [
    "HandlerAndonStartGuard",
    "HandlerSharedBlockerEscalation",
    "NodeSharedBlockerEscalationCompute",
]
