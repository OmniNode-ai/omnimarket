# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative shell; the contract binds the pure standing-rule handler."""

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.standing_rules.model_standing_rules import (
    ModelStandingRulesResult,
    ModelStandingRulesSnapshot,
)


class NodeDispatchStandingRulesCompute(
    NodeCompute[ModelStandingRulesSnapshot, ModelStandingRulesResult]
):
    """Typed node with contract-routed logic."""
