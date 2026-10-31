# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_ruling_recovery import (
    HandlerRulingRecovery,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        HandlerIdleSlotFill,
        HandlerIdleTurnGuard,
    )


def build_handler_ruling_recovery() -> HandlerRulingRecovery:
    """Build the ruling recovery handler."""
    return HandlerRulingRecovery()


def build_handler_idle_slot_fill() -> HandlerIdleSlotFill:
    """Build the idle slot fill handler after its module is loaded."""
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        HandlerIdleSlotFill,
    )

    return HandlerIdleSlotFill()


def build_handler_idle_turn_guard() -> HandlerIdleTurnGuard:
    """Build the idle turn guard after its module is loaded."""
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        HandlerIdleTurnGuard,
    )

    return HandlerIdleTurnGuard()
