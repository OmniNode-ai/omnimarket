# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point and contract-derived MSG topic."""

from pathlib import Path

import yaml

from .handlers.handler_agent_inbox_spool import HandlerAgentInboxSpool


def load_topics() -> tuple[str, ...]:
    """Load the MSG topic the spool reads, as the contract declares it."""
    contract = yaml.safe_load(NodeAgentInboxSpool.contract_path.read_text())
    topics = contract["caller_subscription"]["topics"]
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        raise ValueError("caller_subscription.topics must be a string list")
    return tuple(topics)


class NodeAgentInboxSpool(HandlerAgentInboxSpool):
    """Effect node: read ledger MSG events and spool them for the host's lanes."""

    contract_path = Path(__file__).with_name("contract.yaml")
