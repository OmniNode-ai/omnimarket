# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point and contract-derived fleet topics."""

from pathlib import Path

from omnimarket.nodes.contract_topics import contract_subscribe_topics

from .handlers.handler_host_drift_fleet import HandlerHostDriftFleet


def load_topics() -> tuple[str, ...]:
    """Load heartbeat and resolved topics in their declared order."""
    return contract_subscribe_topics(NodeHostDriftFleet.contract_path)


class NodeHostDriftFleet(HandlerHostDriftFleet):
    """Effect node for bounded fleet reads and projection storage."""

    contract_path = Path(__file__).with_name("contract.yaml")
