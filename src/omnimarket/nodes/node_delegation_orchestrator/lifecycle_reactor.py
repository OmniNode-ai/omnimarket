# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Orchestrator reaction helpers for remote agent lifecycle events."""

from __future__ import annotations

from omnibase_core.enums.enum_agent_task_lifecycle_type import (
    EnumAgentTaskLifecycleType,
)

from omnimarket.nodes.node_delegation_orchestrator.enums import (
    EnumDelegationState,
)


def next_state_from_lifecycle(
    lifecycle: EnumAgentTaskLifecycleType,
) -> EnumDelegationState:
    """Map a remote lifecycle event to the orchestrator FSM."""
    if lifecycle in {
        EnumAgentTaskLifecycleType.SUBMITTED,
        EnumAgentTaskLifecycleType.ACCEPTED,
        EnumAgentTaskLifecycleType.PROGRESS,
        EnumAgentTaskLifecycleType.ARTIFACT,
    }:
        return EnumDelegationState.EXECUTING
    if lifecycle is EnumAgentTaskLifecycleType.COMPLETED:
        return EnumDelegationState.COMPLETED
    return EnumDelegationState.FAILED


__all__ = ["next_state_from_lifecycle"]
