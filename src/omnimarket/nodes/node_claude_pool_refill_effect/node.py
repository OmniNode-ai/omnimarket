# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the Claude pool refill."""

from pathlib import Path

from .handlers.handler_claude_pool_refill import HandlerClaudePoolRefill


class NodeClaudePoolRefillEffect(HandlerClaudePoolRefill):
    """Declarative entry point for filling the lab hosts' Claude token pools."""

    contract_path = Path(__file__).with_name("contract.yaml")
