# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lane stack orphan judgment."""

from pathlib import Path

from .handlers.handler_lane_stack_orphan import HandlerLaneStackOrphan


class NodeLaneStackOrphanCompute(HandlerLaneStackOrphan):
    """Declarative entry point for lane stack orphan operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
