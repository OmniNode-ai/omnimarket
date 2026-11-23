# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delivery that carries no topic is written by the status of the route it arrived on (OMN-26548).

On the dev runtime (omnibase_infra 1e1a83e64) the typed dispatch arm validates the flat wire
object into the envelope with no ``_topic`` and no ``_event_type``, so ``topic`` and ``event_type``
are empty and the writer refused every delivery: ``ValueError: delivery '' is not declared by the
contract``. Each contract route now names its own handler class, which knows the route's status.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers import (
    handler_gateway_workflow_terminal as handler_module,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.handler_gateway_workflow_terminal import (
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
_TENANT = uuid4()
_CORR = uuid4()


def _wire(**overrides: Any) -> dict[str, Any]:
    """The flat wire object exactly as the typed arm hands it over: no injected keys at all."""
    base: dict[str, Any] = {
        "correlation_id": str(_CORR),
        "tenant_id": str(_TENANT),
        "model_used": "glm-5.3-flash",
        "route": "byok-glm-general",
        "provider": "glm",
        "credential_source": "customer_key",
        "content": "",
        "latency_ms": 12,
        "total_tokens": 0,
        "failure_reason": "",
        "terminal_failure_reason": None,
        "rule_evaluations": [],
    }
    base.update(overrides)
    return base


def _handler_of_route(operation: str) -> Any:
    document = yaml.safe_load((_NODE / "contract.yaml").read_text("utf-8"))
    entry = next(
        e
        for e in document["handler_routing"]["handlers"]
        if e["operation"] == operation
    )
    module = importlib.import_module(entry["handler_module"])
    return getattr(module, entry["handler"]["name"])


async def _deliver(operation: str, **overrides: Any) -> tuple[Outcome, FakeRow]:
    handler_class = _handler_of_route(operation)
    row = FakeRow(_TENANT, _CORR)
    handler = handler_class(FakeTerminalStore([row]), FakeDeadLetter())
    request = ModelDelegationTerminalEnvelope.model_validate(_wire(**overrides))
    assert request.topic == ""
    assert request.event_type == ""
    result = await handler.handle(request)
    return result.outcome, row


async def test_failed_route_writes_failed_when_the_delivery_names_no_topic() -> None:
    outcome, row = await _deliver(
        "apply_delegation_failed",
        failure_reason="every rung failed",
        terminal_failure_reason="CredentialUnresolved: CODE_X",
    )
    assert outcome is Outcome.APPLIED
    assert row.status == "failed"


async def test_completed_route_writes_completed_when_the_delivery_names_no_topic() -> (
    None
):
    outcome, row = await _deliver(
        "apply_delegation_completed", content="OK", total_tokens=3
    )
    assert outcome is Outcome.APPLIED
    assert row.status == "completed"


def test_each_contract_route_names_a_handler_of_its_own_status() -> None:
    routes, _ = load_contract_routes()
    document = yaml.safe_load((_NODE / "contract.yaml").read_text("utf-8"))
    names = {}
    for entry in document["handler_routing"]["handlers"]:
        names[entry["topic"]] = entry["handler"]["name"]
        assert entry["handler"]["module"] == entry["handler_module"]
    assert set(names) == set(routes)
    assert len(set(names.values())) == len(names)  # one class per route
    for topic, status in routes.items():
        assert status in names[topic].lower()
