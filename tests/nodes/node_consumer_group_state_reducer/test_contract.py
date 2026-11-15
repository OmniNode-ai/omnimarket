# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer's contract: topics, handler, state machine and overlay (OMN-23992)."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
import yaml

from tests.nodes.node_consumer_group_state_reducer.support import NODE_DIR

pytestmark = pytest.mark.unit

CENSUS_TOPIC = "onex.evt.omnimarket.consumer-group-census.v1"
STATE_TOPIC = "onex.evt.omnimarket.consumer-group-state-updated.v1"


def contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())


def test_topics_and_terminal_event() -> None:
    doc = contract()
    assert doc["event_bus"] == {
        "subscribe_topics": [CENSUS_TOPIC],
        "publish_topics": [STATE_TOPIC],
    }
    assert doc["terminal_event"] == STATE_TOPIC
    assert doc["externally_consumed_topics"] == [STATE_TOPIC]
    assert doc["node_type"] == "reducer"
    assert doc["descriptor"]["purity"] == "pure"


def test_handler_and_models_resolve() -> None:
    doc = contract()
    handler = doc["handler"]
    assert getattr(importlib.import_module(handler["module"]), handler["class"])
    for key in ("input_model", "output_model"):
        model = doc[key]
        assert getattr(importlib.import_module(model["module"]), model["name"])


def test_state_machine_transitions_name_declared_states() -> None:
    machine = contract()["state_machine"]
    names = {s["state_name"] for s in machine["states"]}
    assert names == {"unobserved", "observed"}
    for transition in machine["transitions"]:
        assert transition["from_state"] in names
        assert transition["to_state"] in names
