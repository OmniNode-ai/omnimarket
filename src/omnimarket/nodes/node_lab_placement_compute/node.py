# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for lab host table placement."""

from pathlib import Path

from .handlers.handler_lab_placement import HandlerLabPlacement


class NodeLabPlacementCompute(HandlerLabPlacement):
    """Declarative entry point for lab placement operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
