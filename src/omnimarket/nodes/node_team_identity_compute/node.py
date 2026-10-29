# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the team identity declaration and role resolver."""

from pathlib import Path

from .handlers.handler_team_identity import HandlerTeamIdentity


class NodeTeamIdentityCompute(HandlerTeamIdentity):
    """Declarative entry point for team identity operations."""

    contract_path = Path(__file__).with_name("contract.yaml")
