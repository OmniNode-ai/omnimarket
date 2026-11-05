# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative shell; dispatch resolves the contract's handler."""

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.operator_unanswered import (
    ModelUnansweredRequest,
    ModelUnansweredResult,
)


class NodeOperatorUnansweredCompute(
    NodeCompute[ModelUnansweredRequest, ModelUnansweredResult]
):
    """Compute structural coverage from a supplied transcript and arrival rows."""
