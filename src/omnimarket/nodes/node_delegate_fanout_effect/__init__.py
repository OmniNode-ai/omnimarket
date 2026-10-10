# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation fanout effect node (OMN-20678).

EFFECT: admits labeled delegation tasks, publishes each admitted task once through
the existing delegation client to node_delegate_skill_orchestrator, and renders
every receipt or refusal. No caller retries.
"""

from omnimarket.nodes.node_delegate_fanout_effect.handlers.handler_delegate_fanout import (
    HandlerDelegateFanout,
)
from omnimarket.nodes.node_delegate_fanout_effect.models.model_fanout_request import (
    ModelFanoutItem,
    ModelFanoutRequest,
)
from omnimarket.nodes.node_delegate_fanout_effect.models.model_fanout_result import (
    ModelFanoutResult,
    ModelFanoutRow,
)


class NodeDelegateFanoutEffect(HandlerDelegateFanout):
    """ONEX entry-point wrapper for HandlerDelegateFanout."""


__all__ = [
    "HandlerDelegateFanout",
    "ModelFanoutItem",
    "ModelFanoutRequest",
    "ModelFanoutResult",
    "ModelFanoutRow",
    "NodeDelegateFanoutEffect",
]
