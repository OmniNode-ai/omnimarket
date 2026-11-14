# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the compose project census."""

from pathlib import Path

from .handlers.handler_compose_project_census import HandlerComposeProjectCensus


class NodeComposeProjectCensusCompute(HandlerComposeProjectCensus):
    """Declarative entry point for the compose project census."""

    contract_path = Path(__file__).with_name("contract.yaml")
