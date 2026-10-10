# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_fixer_dispatcher publishes only to topics a contract subscribes to.

The dispatcher's command topics (routing.command_topics) and publish topics
must each have a subscriber in some omnimarket node contract, unless the
dispatcher declares the topic in externally_consumed_topics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_NODES_DIR = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_DISPATCHER = "node_fixer_dispatcher"


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


def _subscribed_topics() -> set[str]:
    topics: set[str] = set()
    for contract in _NODES_DIR.glob("node_*/contract.yaml"):
        bus = _load(contract).get("event_bus") or {}
        topics.update(bus.get("subscribe_topics") or [])
    return topics


def _dispatcher_published_topics() -> set[str]:
    data = _load(_NODES_DIR / _DISPATCHER / "contract.yaml")
    published = set((data.get("event_bus") or {}).get("publish_topics") or [])
    published.update((data.get("routing") or {}).get("command_topics", {}).values())
    return published - set(data.get("externally_consumed_topics") or [])


@pytest.mark.unit
def test_dispatcher_published_topics_all_have_a_subscriber() -> None:
    orphans = sorted(_dispatcher_published_topics() - _subscribed_topics())
    assert not orphans, (
        f"{_DISPATCHER} publishes topics no omnimarket contract subscribes to: "
        f"{orphans}"
    )


@pytest.mark.unit
def test_positive_control_command_topics_are_collected() -> None:
    published = _dispatcher_published_topics()
    assert published, "dispatcher declares no command topics; the check is vacuous"
    assert "onex.cmd.omnimarket.fixer-dispatch-start.v1" in _subscribed_topics()
