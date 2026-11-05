# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative runner-local retention node."""

from pathlib import Path

from omnimarket.nodes.node_runner_workspace_retention_effect.handlers.handler_runner_retention import (
    HandlerRunnerRetention,
)


class NodeRunnerWorkspaceRetentionEffect(HandlerRunnerRetention):
    """Declarative entry point for the contract-bound retention handler."""

    contract_path = Path(__file__).with_name("contract.yaml")
