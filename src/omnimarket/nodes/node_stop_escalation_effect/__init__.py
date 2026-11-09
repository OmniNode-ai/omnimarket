# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stop escalation effect: one escalation row and one alarm with a delivery receipt for a stop not cleared in time.

``HandlerStopEscalation.handle(ModelStopEscalationRequest) -> ModelStopEscalationResult``; the
time is the stop reducer contract's ``stale_after_hours``.
"""

from omnimarket.nodes.node_stop_escalation_effect.handlers.handler_stop_escalation import (
    HandlerStopEscalation,
)

__all__ = ["HandlerStopEscalation"]
