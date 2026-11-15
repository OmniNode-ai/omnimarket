# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_panel_answer_effect: the floor's panel answers on the bus (OMN-19998)."""

from omnimarket.nodes.node_floor_panel_answer_effect.handlers.handler_floor_panel_answer import (
    HandlerFloorPanelAnswer,
)

__all__ = ["HandlerFloorPanelAnswer", "NodeFloorPanelAnswerEffect"]


class NodeFloorPanelAnswerEffect(HandlerFloorPanelAnswer):
    """ONEX entry-point wrapper for HandlerFloorPanelAnswer."""
