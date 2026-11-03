# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21226 (hot-loading plan task H1): the registry declares the topics it owns.

The accepted topic is the registry's compacted state: a record that ages off it can
never be rebuilt, so the provisioner must create it compacted, with a one-hour
minimum compaction lag. The replies topic is one reply per request and is time
retained (delete). Neither has a published_events route here: the relay publishes
them (H4), so the contract declares the topics and nothing that would claim a
publish the node does not make.

Failure modes, each with a test below:
  T1  the accepted topic is not declared, or declares another cleanup.policy, so
      the provisioner creates it time-retained
  T2  the accepted topic declares no min.compaction.lag.ms of one hour
  T3  the replies topic is not declared, or is compacted instead of deleted
  T4  a topic is claimed as a published_events route this node never publishes
  T5  a topic is declared twice
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.registry_compaction_source import (
    _declared_cleanup_policies,
    _topic_declarations,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_contract_registry"
    / "contract.yaml"
)
ACCEPTED = "onex.evt.platform.contract-hotload-accepted.v1"
REPLIES = "onex.evt.platform.contract-hotload-replies.v1"
ONE_HOUR_MS = "3600000"


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _kafka_config(topic: str) -> dict[str, Any]:
    matches = [
        item
        for name, item in _topic_declarations(_contract())
        if name == topic and isinstance(item.get("topic_config"), dict)
    ]
    assert len(matches) == 1, f"{topic} must be declared once with a topic_config"
    topic_config = matches[0]["topic_config"]
    assert isinstance(topic_config, dict)
    kafka_config = topic_config.get("kafka_config")
    assert isinstance(kafka_config, dict)
    return kafka_config


def test_t1_the_accepted_topic_is_declared_compacted() -> None:
    assert _kafka_config(ACCEPTED)["cleanup.policy"] == "compact"
    declared = _declared_cleanup_policies([("node_contract_registry", _contract())])
    assert declared[ACCEPTED] == "compact"


def test_t2_the_accepted_topic_keeps_a_record_for_one_hour_before_compaction() -> None:
    assert _kafka_config(ACCEPTED)["min.compaction.lag.ms"] == ONE_HOUR_MS


def test_t3_the_replies_topic_is_declared_with_delete_retention() -> None:
    config = _kafka_config(REPLIES)
    assert config["cleanup.policy"] == "delete"
    assert "min.compaction.lag.ms" not in config


@pytest.mark.parametrize("topic", [ACCEPTED, REPLIES])
def test_t4_no_published_events_route_names_an_owned_topic(topic: str) -> None:
    contract = _contract()
    for key in ("published_events", "produced_events", "consumed_events"):
        routes = contract.get(key) or []
        assert topic not in {
            value
            for item in routes
            if isinstance(item, dict)
            for value in (item.get("topic"), item.get("name"))
        }


def test_t5_each_owned_topic_is_declared_exactly_once() -> None:
    names = [name for name, _ in _topic_declarations(_contract())]
    assert names.count(ACCEPTED) == 1
    assert names.count(REPLIES) == 1
