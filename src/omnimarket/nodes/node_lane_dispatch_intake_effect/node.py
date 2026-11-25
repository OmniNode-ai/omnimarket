# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entry-point wrapper for HandlerLaneDispatchIntake."""

from omnimarket.nodes.node_lane_dispatch_intake_effect.handlers.handler_lane_dispatch_intake import (
    HandlerLaneDispatchIntake,
)


class NodeLaneDispatchIntakeEffect(HandlerLaneDispatchIntake):
    """Reads MSG rows to=labdispatch and places each carried lane, one at a time."""
