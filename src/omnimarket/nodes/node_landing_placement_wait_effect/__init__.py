# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify deadline kills using placement time and bound missing-result gates."""

from pathlib import Path

from .handlers.handler_landing_placement_wait import HandlerLandingPlacementWait


class NodeLandingPlacementWaitEffect(HandlerLandingPlacementWait):
    """ONEX entry point for the local handler."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = ["HandlerLandingPlacementWait", "NodeLandingPlacementWaitEffect"]
