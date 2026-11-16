# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ingest node's result event is a declared output, not a dropped one (OMN-24932).

The runtime logged "no result applier wired" on every dispatch because the contract named the success event as a
terminal event while publish_topics stayed empty: the handler's output was dropped and the merged-change reading had
no result to see. Each test fails on the empty-publish shape and passes once the topic is declared, published to by
the effects principal and consumed outside the node.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

RESULT_TOPIC = "onex.evt.omnimarket.host-table-keys-ingested.v1"
EFFECTS_PRINCIPAL = "${ONEX_ACL_DEV_RUNTIME_EFFECTS_PRINCIPAL}"


def contract() -> dict[str, Any]:
    package = importlib.import_module(
        "omnimarket.nodes.node_host_table_keys_ingest_effect"
    )
    assert package.__file__ is not None
    loaded = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    assert isinstance(loaded, dict)
    return loaded


def test_terminal_success_event_is_a_published_topic() -> None:
    declared = contract()
    assert declared["runtime_dispatch"]["terminal_events"]["success"] == RESULT_TOPIC
    assert declared["terminal_event"] == RESULT_TOPIC
    assert declared["event_bus"]["publish_topics"] == [RESULT_TOPIC]


def test_result_topic_is_declared_as_consumed_outside_the_node() -> None:
    assert contract()["externally_consumed_topics"] == [RESULT_TOPIC]


def test_effects_principal_may_write_and_describe_the_result_topic() -> None:
    grants = {
        (grant["name"], grant["operation"])
        for entry in contract()["broker_acl_grants"]
        if entry["principal"] == EFFECTS_PRINCIPAL
        for grant in entry["grants"]
    }
    assert (RESULT_TOPIC, "WRITE") in grants
    assert (RESULT_TOPIC, "DESCRIBE") in grants


def test_every_terminal_event_is_published() -> None:
    declared = contract()
    terminals = set(declared["runtime_dispatch"]["terminal_events"].values())
    assert terminals <= set(declared["event_bus"]["publish_topics"])
