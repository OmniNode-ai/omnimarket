# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record refused dispatches and refund their dispatch budgets without recording worker failures."""

from pathlib import Path

from .handlers.handler_landing_not_started import HandlerLandingNotStarted


class NodeLandingNotStartedEffect(HandlerLandingNotStarted):
    """ONEX entry point for the local handler."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = ["HandlerLandingNotStarted", "NodeLandingNotStartedEffect"]
