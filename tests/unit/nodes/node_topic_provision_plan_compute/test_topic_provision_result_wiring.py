# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plan node's contract carries what the runtime needs to deliver the returned plan.

The runtime wires a result applier for a contract only when the contract declares a
publish topic and a published_events map. A contract with neither runs its handler and
then routes the returned plan to the dead letter queue. `onex run-node` also refuses a
contract with no top-level terminal_event.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    load_published_events_map,
)

from omnimarket.models.topic_provision.model_topic_provision_plan import (
    ModelTopicProvisionPlan,
)

pytestmark = pytest.mark.unit

CONTRACT_PATH = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_topic_provision_plan_compute"
    / "contract.yaml"
)
COMPLETED_TOPIC = "onex.evt.omnimarket.topic-provision-plan-completed.v1"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_terminal_event_is_the_completed_topic_under_runtime_dispatch() -> None:
    contract = _contract()
    dispatch = contract["runtime_dispatch"]
    assert isinstance(dispatch, dict)
    assert contract["terminal_event"] == COMPLETED_TOPIC
    assert dispatch["terminal_events"]["success"] == COMPLETED_TOPIC


def test_completed_topic_is_a_publish_topic() -> None:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    assert event_bus["publish_topics"] == [COMPLETED_TOPIC]


def test_published_events_map_routes_the_returned_plan_to_the_completed_topic() -> None:
    mapping = load_published_events_map(CONTRACT_PATH)
    assert mapping == {ModelTopicProvisionPlan.__name__: COMPLETED_TOPIC}


def test_runtime_login_is_granted_write_and_describe_on_the_completed_topic() -> None:
    grants = _contract()["broker_acl_grants"]
    assert isinstance(grants, list)
    assert len(grants) == 1
    assert grants[0]["principal"] == "${ONEX_ACL_RUNTIME_PRINCIPAL}"
    rows = {(g["name"], g["operation"]) for g in grants[0]["grants"]}
    assert rows == {(COMPLETED_TOPIC, "WRITE"), (COMPLETED_TOPIC, "DESCRIBE")}
