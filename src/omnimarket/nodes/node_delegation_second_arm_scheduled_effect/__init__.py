# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delegation_second_arm_scheduled_effect: one run of the second local benchmark arm on the runtime tick (OMN-25052)."""

from omnimarket.nodes.node_delegation_second_arm_scheduled_effect.handlers.handler_second_arm_scheduled import (
    HandlerSecondArmScheduled,
)

__all__ = ["HandlerSecondArmScheduled", "NodeDelegationSecondArmScheduledEffect"]


class NodeDelegationSecondArmScheduledEffect(HandlerSecondArmScheduled):
    """ONEX entry-point wrapper for HandlerSecondArmScheduled."""
