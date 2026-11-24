# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point of the agent message effect node."""

from pathlib import Path

from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
    HandlerAgentMessage,
)


class NodeAgentMessageEffect(HandlerAgentMessage):
    """Effect node: send and read agent messages on the one declared topic."""

    contract_path = Path(__file__).with_name("contract.yaml")
