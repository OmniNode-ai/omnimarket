# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation failure explanation compute: a readable account marked commentary, never evidence (OMN-18538)."""

from omnimarket.nodes.node_delegation_failure_explanation_compute.handlers.handler_delegation_failure_explanation import (
    HandlerDelegationFailureExplanation,
)


class NodeDelegationFailureExplanationCompute(HandlerDelegationFailureExplanation):
    """ONEX entrypoint for the failure-explanation compute."""


__all__ = [
    "HandlerDelegationFailureExplanation",
    "NodeDelegationFailureExplanationCompute",
]
