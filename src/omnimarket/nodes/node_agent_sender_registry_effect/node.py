# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point of the agent sender registry effect node."""

from pathlib import Path

from omnimarket.nodes.node_agent_sender_registry_effect.handlers.handler_agent_sender_registry import (
    HandlerAgentSenderRegistry,
)


class NodeAgentSenderRegistryEffect(HandlerAgentSenderRegistry):
    """Effect node: register, list and revoke one agent sender and its key in the settings store."""

    contract_path = Path(__file__).with_name("contract.yaml")
