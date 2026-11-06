# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ItemLaneMiss compute node."""

from .handlers.handler_item_lane_miss import HandlerItemLaneMiss


class NodeItemLaneMissCompute(HandlerItemLaneMiss):
    """Declarative runtime entry point."""
