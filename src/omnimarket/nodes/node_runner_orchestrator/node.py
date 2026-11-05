# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the existing runner management node."""

from pathlib import Path

from .handlers.handler_runner_orchestrator import HandlerRunnerOrchestrator


class NodeRunnerOrchestrator(HandlerRunnerOrchestrator):
    """Runner actions and read-only watch routes live in this node's contract."""

    contract_path = Path(__file__).with_name("contract.yaml")
