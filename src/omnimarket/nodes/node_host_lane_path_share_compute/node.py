# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lane path share check."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_lane_path_share_compute.handlers.handler_lane_path_share import (
    HandlerLanePathShare,
)


class NodeHostLanePathShareCompute(HandlerLanePathShare):
    """Declarative entry point for the lane path share check."""

    contract_path = Path(__file__).with_name("contract.yaml")
