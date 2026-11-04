# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture builders for the network graph compute tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.models.network_graph import (
    ModelContractText,
    ModelNetworkGraphOverlay,
    ModelNetworkGraphRequest,
)

REFERENCE_TIME = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

# Overlay values are handed to the node; the tests pick their own.
OVERLAY = ModelNetworkGraphOverlay(
    command_segment="cmd",
    domain_segment_index=2,
    window=timedelta(hours=24),
)

PUBLISH_TOPIC = "t.fixture.published"
SUBSCRIBE_TOPIC = "t.fixture.subscribed"


def contract(repo: str, path: str, text: str) -> ModelContractText:
    return ModelContractText(repo=repo, path=path, text=text)


def node_text(name: str, body: str = "") -> str:
    return f"name: {name}\nnode_type: compute\n{body}"


def request(*contracts: ModelContractText) -> ModelNetworkGraphRequest:
    return ModelNetworkGraphRequest(
        contracts=contracts, overlay=OVERLAY, reference_time=REFERENCE_TIME
    )


def bus_text(name: str, publishes: tuple[str, ...], subscribes: tuple[str, ...]) -> str:
    pub = "".join(f"    - {t}\n" for t in publishes)
    sub = "".join(f"    - {t}\n" for t in subscribes)
    return node_text(
        name,
        "event_bus:\n"
        + (f"  publish_topics:\n{pub}" if pub else "  publish_topics: []\n")
        + (f"  subscribe_topics:\n{sub}" if sub else "  subscribe_topics: []\n"),
    )
