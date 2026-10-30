# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Packaged entry point for node_lab_placement_compute."""

from .handlers import HandlerLabPlacement
from .node import NodeLabPlacementCompute

__all__ = ["HandlerLabPlacement", "NodeLabPlacementCompute"]
