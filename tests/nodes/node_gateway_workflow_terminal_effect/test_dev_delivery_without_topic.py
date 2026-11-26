# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal writer applies the delivery the dev runtime hands it, which names no topic.

The runtime's standard dispatch callback validates the unwrapped wire payload into the handler's
input model, so an operation_match handler without an event_model is handed
``ModelDelegationTerminalEnvelope(topic="", event_type="", payload=<flat wire>)``. The fixture
records that delivery and the log line it produced on the dev lane at 2026-11-23T05:04:13Z.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers import (
    handler_gateway_workflow_terminal as handler_module,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.handler_gateway_workflow_terminal import (
    HandlerGatewayWorkflowTerminal,
    load_contract_routes,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_delegation_terminal_envelope import (
    ModelDelegationTerminalEnvelope,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalOutcome as Outcome,
)
from tests.nodes.node_gateway_workflow_terminal_effect.fakes import (
    FakeDeadLetter,
    FakeRow,
    FakeTerminalStore,
)

_NODE = Path(handler_module.__file__).resolve().parents[1]
_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "dev_delivery_20261123T0504Z.json").read_text(
        "utf-8"
    )
)
_ROUTES, _ = load_contract_routes()
_TENANT = UUID(_FIXTURE["delivery"]["tenant_id"])
_CORR = UUID(_FIXTURE["delivery"]["correlation_id"])


def _recorded_delivery() -> ModelDelegationTerminalEnvelope:
    """The envelope the dev dispatch callback builds: the flat wire dict, no injected key."""
    request = ModelDelegationTerminalEnvelope.model_validate(dict(_FIXTURE["delivery"]))
    assert (request.topic, request.event_type) == ("", "")
    assert (
        sorted(request.payload)
        == _FIXTURE["dispatch_boundary"]["handler_receives"]["payload_keys"]
    )
    return request


def _route_handlers() -> list[tuple[str, str, Any]]:
    """Each declared topic with the status it carries and the handler class its route names."""
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text("utf-8"))
    built = []
    for entry in contract["handler_routing"]["handlers"]:
        module = importlib.import_module(entry["handler"]["module"])
        cls = getattr(module, entry["handler"]["name"])
        built.append((entry["topic"], _ROUTES[entry["topic"]], cls))
    return built


_STATUS_ROUTES = [r for r in _route_handlers() if not r[2].refusal_only]
_REFUSAL_ROUTES = [r for r in _route_handlers() if r[2].refusal_only]


@pytest.mark.parametrize(("topic", "status", "cls"), _STATUS_ROUTES)
async def test_recorded_dev_delivery_is_applied_by_the_route_handler(
    topic: str, status: str, cls: Any
) -> None:
    store = FakeTerminalStore([FakeRow(_TENANT, _CORR)])
    handler = cls(store, FakeDeadLetter())
    result = await handler.handle(_recorded_delivery())
    assert result.outcome is Outcome.APPLIED, topic
    assert store.rows[0].status == status


@pytest.mark.parametrize(("topic", "status", "cls"), _REFUSAL_ROUTES)
async def test_the_refusal_route_leaves_a_recorded_terminal_that_is_no_refusal(
    topic: str, status: str, cls: Any
) -> None:
    store = FakeTerminalStore([FakeRow(_TENANT, _CORR)])
    result = await cls(store, FakeDeadLetter()).handle(_recorded_delivery())
    assert result.outcome is Outcome.NOT_A_GATEWAY_WORKFLOW, topic
    assert store.rows[0].status == "published"


async def test_undeclared_topic_still_raises_on_every_route_handler() -> None:
    for _, _, cls in _route_handlers():
        handler = cls(FakeTerminalStore([FakeRow(_TENANT, _CORR)]), FakeDeadLetter())
        request = ModelDelegationTerminalEnvelope(
            topic="x.y", payload=dict(_FIXTURE["delivery"])
        )
        with pytest.raises(ValueError, match="not declared"):
            await handler.handle(request)


async def test_unbound_handler_still_refuses_a_delivery_that_names_nothing() -> None:
    handler = HandlerGatewayWorkflowTerminal(
        FakeTerminalStore([FakeRow(_TENANT, _CORR)]), FakeDeadLetter()
    )
    with pytest.raises(
        ValueError, match=r"delivery '' is not declared by the contract"
    ):
        await handler.handle(_recorded_delivery())


def test_each_declared_topic_names_its_own_handler_class() -> None:
    classes = [cls for _, _, cls in _route_handlers()]
    assert len(set(classes)) == len(classes) == 3
    assert len(_REFUSAL_ROUTES) == 1
