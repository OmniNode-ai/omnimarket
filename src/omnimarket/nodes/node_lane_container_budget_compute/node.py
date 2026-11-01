# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the lane application container budget judgment."""

from pathlib import Path

from .handlers.handler_lane_container_budget import HandlerLaneContainerBudget


class NodeLaneContainerBudgetCompute(HandlerLaneContainerBudget):
    """Declarative entry point for lane container budget operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
