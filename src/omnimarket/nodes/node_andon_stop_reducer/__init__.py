# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Andon stop reducer: fold writer events into a stop and decide admission into its scope.

``HandlerAndonStop.handle(ModelStopFoldRequest) -> ModelStopFoldResult``; the transitions are
the contract's ``state_machine``, run by ``omnimarket.handlers.rules_stop_gate``.
"""

from omnimarket.nodes.node_andon_stop_reducer.handlers.handler_andon_stop import (
    HandlerAndonStop,
)
from omnimarket.nodes.node_andon_stop_reducer.handlers.handler_stop_gate import (
    HandlerStopGate,
)

__all__ = ["HandlerAndonStop", "HandlerStopGate"]
