# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract wires the node to the bus: a demand on the requested topic reaches it and the run-completed result leaves it."""

from __future__ import annotations

from pathlib import Path

import yaml

import omnimarket.nodes.node_host_local_config_drift_compute as node_package

CONTRACT = Path(node_package.__file__).resolve().parent / "contract.yaml"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_subscribes_to_the_requested_topic_and_publishes_the_terminal_event() -> None:
    contract = _contract()
    topics = contract["topics"]
    event_bus = contract["event_bus"]
    assert isinstance(topics, dict)
    assert isinstance(event_bus, dict)
    assert event_bus["subscribe_topics"] == [
        topics["host_local_config_drift_requested"]
    ]
    assert event_bus["publish_topics"] == [contract["terminal_event"]]
    assert contract["terminal_event"] == topics["host_local_config_drift_run_completed"]


def test_runtime_dispatch_names_the_command_topic() -> None:
    contract = _contract()
    dispatch = contract["runtime_dispatch"]
    topics = contract["topics"]
    assert isinstance(dispatch, dict)
    assert isinstance(topics, dict)
    assert dispatch["command_topic"] == topics["host_local_config_drift_requested"]


def test_handler_routing_matches_the_command_model() -> None:
    contract = _contract()
    routing = contract["handler_routing"]
    assert isinstance(routing, dict)
    assert routing["routing_strategy"] == "payload_type_match"
    handlers = routing["handlers"]
    assert isinstance(handlers, list)
    assert len(handlers) == 1
    entry = handlers[0]
    assert entry["event_model"]["name"] == "ModelLocalConfigDriftCommand"
    assert entry["handler"]["name"] == "HandlerHostLocalConfigDrift"


def test_runtime_principal_may_read_the_command_and_write_the_result() -> None:
    contract = _contract()
    topics = contract["topics"]
    assert isinstance(topics, dict)
    grants = contract["broker_acl_grants"]
    assert isinstance(grants, list)
    assert len(grants) == 1
    assert grants[0]["principal"] == "${ONEX_ACL_RUNTIME_PRINCIPAL}"
    granted = {(item["name"], item["operation"]) for item in grants[0]["grants"]}
    command = topics["host_local_config_drift_requested"]
    result = topics["host_local_config_drift_run_completed"]
    assert granted == {
        (command, "READ"),
        (command, "DESCRIBE"),
        (result, "WRITE"),
        (result, "DESCRIBE"),
    }
