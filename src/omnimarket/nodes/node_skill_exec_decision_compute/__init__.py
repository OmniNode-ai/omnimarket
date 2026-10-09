# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill-exec decision compute node (OMN-20686)."""

from omnimarket.nodes.node_skill_exec_decision_compute.handlers.handler_skill_exec_decision import (
    HandlerSkillExecDecision,
)


class NodeSkillExecDecisionCompute(HandlerSkillExecDecision):
    """ONEX entrypoint for the decisions of the self-running ledger skills' executor."""


__all__ = ["HandlerSkillExecDecision", "NodeSkillExecDecisionCompute"]
