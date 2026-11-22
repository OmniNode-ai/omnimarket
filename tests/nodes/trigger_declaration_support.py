# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read how a node contract declares what triggers it (a merged node nothing triggers is a defect).

A command topic a node subscribes to has a trigger when another node contract publishes it, or the
contract names its external producer in ``externally_produced_topics`` (a bare topic is refused by the
topic graph validator, so the producer must be named). A node on the runtime tick or with a ``schedule``
block is scheduled. A node that is CI-only by design says so in ``ci_only.reason``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
NODES = REPO / "src" / "omnimarket" / "nodes"
RUNTIME_TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"


def load_contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def command_topic(contract: dict[str, Any]) -> str:
    topic = (contract.get("runtime_dispatch") or {}).get("command_topic")
    assert isinstance(topic, str), "the contract declares no command topic"
    assert topic, "the contract declares an empty command topic"
    return topic


def named_external_producer(contract: dict[str, Any], topic: str) -> str | None:
    """The producer the contract names for ``topic`` in externally_produced_topics, if any."""
    for entry in contract.get("externally_produced_topics") or []:
        if isinstance(entry, dict) and entry.get("topic") == topic:
            producer = entry.get("producer")
            if isinstance(producer, str) and producer.strip():
                return producer
    return None


def publishing_nodes(topic: str) -> list[str]:
    """Every node contract whose event_bus.publish_topics lists ``topic``."""
    found: list[str] = []
    for path in sorted(NODES.glob("node_*/contract.yaml")):
        loaded = yaml.safe_load(path.read_text("utf-8"))
        if not isinstance(loaded, dict):
            continue
        published = (loaded.get("event_bus") or {}).get("publish_topics") or []
        if topic in published:
            found.append(path.parent.name)
    return found


def subscribes_to_runtime_tick(contract: dict[str, Any]) -> bool:
    return RUNTIME_TICK_TOPIC in (
        (contract.get("event_bus") or {}).get("subscribe_topics") or []
    )
