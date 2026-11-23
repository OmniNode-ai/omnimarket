# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The on-demand seams of the host hygiene orchestrator (OMN-24610): each fails when either side changes shape.

S6 contract first topic -> onex run-node client: the command topic the client publishes on is the contract's first subscribed topic and the route topic of the on-demand operation, not the host disk read topic.
S7 client payload -> request model: the payload the client normalizes validates as the request model and carries the caller's correlation id.
S8 run event -> client's correlation match: the terminal topic the client polls is the contract's terminal event, and the run event's correlation id is the caller's.
"""

from __future__ import annotations

import importlib
import typing
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from omnibase_core.cli.cli_run_node import _normalize_payload, _resolve_node_topics

from omnimarket.models.model_host_hygiene_run_requested import (
    ModelHostHygieneRunRequested,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    run_completed_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.handler_host_hygiene_run_requested import (
    HandlerHostHygieneRunRequested,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"
NODE = "node_host_hygiene_orchestrator"


def _contract(node: str) -> dict[str, Any]:
    return yaml.safe_load((NODES / node / "contract.yaml").read_text())


CONTRACT = _contract(NODE)
ROUTES = {r["operation"]: r for r in CONTRACT["handler_routing"]["handlers"]}


def _acl_names(operation: str) -> set[str]:
    rows = CONTRACT["broker_acl_grants"][0]["grants"]
    return {
        r["name"]
        for r in rows
        if r["resource_type"] == "TOPIC" and r["operation"] == operation
    }


def test_s6_the_command_topic_is_the_first_subscribed_topic_and_the_on_demand_route() -> (
    None
):
    _, command_topic, _, inject = _resolve_node_topics(NODE)
    assert command_topic == CONTRACT["event_bus"]["subscribe_topics"][0]
    assert command_topic == ROUTES["host_hygiene.run_requested"]["topic"]
    assert command_topic != ROUTES["record_host_disk_read"]["topic"]
    assert inject is True


def test_s6_the_response_topic_is_the_contract_terminal_event() -> None:
    _, _, response_topic, _ = _resolve_node_topics(NODE)
    assert response_topic == CONTRACT["terminal_event"]
    assert response_topic == run_completed_topic()


def test_s7_the_normalized_payload_validates_as_the_request_model() -> None:
    cid = UUID(int=1)
    _, _, _, inject = _resolve_node_topics(NODE)
    payload = _normalize_payload({}, cid, inject)
    model = ModelHostHygieneRunRequested.model_validate(payload)
    assert model.correlation_id == cid


def test_s7_the_route_event_model_is_the_request_model() -> None:
    spec = ROUTES["host_hygiene.run_requested"]["event_model"]
    model = getattr(importlib.import_module(spec["module"]), spec["name"])
    assert model is ModelHostHygieneRunRequested


def test_s7_the_route_handler_is_the_on_demand_handler_and_its_request_is_the_model() -> (
    None
):
    spec = ROUTES["host_hygiene.run_requested"]["handler"]
    handler = getattr(importlib.import_module(spec["module"]), spec["name"])
    assert handler is HandlerHostHygieneRunRequested
    hints = typing.get_type_hints(handler.handle)
    assert hints["request"] is ModelHostHygieneRunRequested


def test_s8_the_broker_acl_grants_cover_the_request_and_terminal_topics() -> None:
    request_topic = ROUTES["host_hygiene.run_requested"]["topic"]
    terminal_topic = CONTRACT["terminal_event"]
    assert request_topic in _acl_names("READ")
    assert request_topic in _acl_names("DESCRIBE")
    assert terminal_topic in _acl_names("WRITE")
