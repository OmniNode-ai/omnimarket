# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cache judging-path observations and ledger row indexes between controller invocations."""

from pathlib import Path

from .handlers.handler_landing_tick_speed import HandlerLandingTickSpeed


class NodeLandingTickSpeedEffect(HandlerLandingTickSpeed):
    """ONEX entry point for the local handler."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = ["HandlerLandingTickSpeed", "NodeLandingTickSpeedEffect"]
