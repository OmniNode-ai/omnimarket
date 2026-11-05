# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entrypoint for the idle-turn guard decision, bound to its handler by contract."""

from omnimarket.nodes.node_idle_turn_guard_compute.handlers.handler_idle_turn_guard_decision import (
    HandlerIdleTurnGuardDecision,
)


class NodeIdleTurnGuardCompute(HandlerIdleTurnGuardDecision):
    """Pure compute: a turn's finding lines and the adapter's facts in, a typed verdict out."""


__all__ = ["NodeIdleTurnGuardCompute"]
