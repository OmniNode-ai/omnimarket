# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the landing policy."""

from pathlib import Path

from .handlers.handler_landing_policy import HandlerLandingPolicy


class NodeLandingPolicyCompute(HandlerLandingPolicy):
    """Declarative entry point for landing policy judgements."""

    contract_path = Path(__file__).with_name("contract.yaml")
