# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for landing placement."""

from pathlib import Path

from .handlers.handler_landing_placement import HandlerLandingPlacement


class NodeLandingPlacementEffect(HandlerLandingPlacement):
    """Declarative entry point: where a landing worker runs."""

    contract_path = Path(__file__).with_name("contract.yaml")
