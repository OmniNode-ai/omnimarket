# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point and contract-derived fleet topics."""

from pathlib import Path

import yaml

from .handlers.handler_host_drift_fleet import HandlerHostDriftFleet


def load_topics() -> tuple[str, ...]:
    """Load heartbeat and resolved topics in their declared order."""
    contract = yaml.safe_load(NodeHostDriftFleet.contract_path.read_text())
    topics = contract["caller_subscription"]["topics"]
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        raise ValueError("caller_subscription.topics must be a string list")
    return tuple(topics)


class NodeHostDriftFleet(HandlerHostDriftFleet):
    """Effect node for bounded fleet reads and projection storage."""

    contract_path = Path(__file__).with_name("contract.yaml")
