# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state floor projection topic is declared under the producing node's ``publish_topics``.

The runtime boot provisioner creates a topic from a contract's ``event_bus.publish_topics``. A topic listed only under
``externally_consumed_topics`` is never provisioned, so the scheduled publish has no topic to land on. The topic name is read
from the node's own ``runtime.publish.topic``, never copied here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_work_state_projection_effect"
    / "contract.yaml"
)


def _contract() -> dict[str, Any]:
    data = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _publish_topic(contract: dict[str, Any]) -> str:
    topic = contract["runtime"]["publish"]["topic"]
    assert isinstance(topic, str)
    assert topic
    return topic


def test_publish_topic_is_in_event_bus_publish_topics() -> None:
    contract = _contract()
    assert _publish_topic(contract) in contract["event_bus"]["publish_topics"]


def test_publish_topic_stays_externally_consumed() -> None:
    contract = _contract()
    assert _publish_topic(contract) in contract["externally_consumed_topics"]
