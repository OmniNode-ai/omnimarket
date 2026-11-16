# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Goal-scope admission compute node (OMN-23851)."""

from omnimarket.nodes.node_goal_scope_admission_compute.handlers.handler_goal_scope_admission import (
    HandlerGoalScopeAdmission,
)


class NodeGoalScopeAdmissionCompute(HandlerGoalScopeAdmission):
    """ONEX entrypoint for the one goal-scope admission rule."""


__all__ = ["HandlerGoalScopeAdmission", "NodeGoalScopeAdmissionCompute"]
