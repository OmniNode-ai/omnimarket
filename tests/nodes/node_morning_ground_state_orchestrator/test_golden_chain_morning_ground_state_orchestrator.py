# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves, its handler runs a typed request to a typed terminal.

Decision parity: the nine recorded scenarios of the original workflow (fresh,
re-delivered, peer-owned, unavailable precheck, force, dry, custom, partial, empty)
produce the same terminal, the same phase order and the same model/effort routing.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import JsonValue

from omnimarket.nodes.node_morning_ground_state_orchestrator.models import (
    ModelMorningGroundStateRequest,
    ModelMorningGroundStateResult,
)

from .helpers import NODE, FixtureGateway, public_overlay, request

PARITY = json.loads(
    (Path(__file__).parent / "fixtures" / "decision_parity.json").read_text()
)
PACKAGE = f"omnimarket.nodes.{NODE}"


def _contract() -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        yaml.safe_load(files(PACKAGE).joinpath("contract.yaml").read_text()),
    )


def test_golden_chain_morning_ground_state_orchestrator() -> None:
    contract = _contract()
    assert contract["name"] == NODE
    assert contract["node_type"] == "orchestrator"
    routing = cast(dict[str, JsonValue], contract["handler_routing"])
    assert routing["routing_strategy"] == "operation_match"
    entries = cast(list[dict[str, JsonValue]], routing["handlers"])
    assert [entry["operation"] for entry in entries] == ["morning_ground_state"]
    entry = entries[0]
    handler_ref = cast(dict[str, str], entry["handler"])
    handler_type = getattr(
        importlib.import_module(handler_ref["module"]), handler_ref["name"]
    )
    input_module, _, input_name = str(entry["input_model"]).rpartition(".")
    output_module, _, output_name = str(entry["output_model"]).rpartition(".")
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)
    assert request_type is ModelMorningGroundStateRequest
    assert result_type is ModelMorningGroundStateResult
    # Definition-B: one typed request in, one typed result out.
    assert inspect.iscoroutinefunction(handler_type.handle)
    parameters = list(inspect.signature(handler_type.handle).parameters)
    assert parameters == ["self", "request"]
    gateway = FixtureGateway()
    handler = handler_type(None, gateway, public_overlay())
    result = asyncio.run(handler.handle(request()))
    assert isinstance(result, result_type)
    assert result.phases_run == ["GroundState", "Triage", "Reconcile", "Integrate"]
    assert gateway.calls[-1]["phase"] == "Goal"


def test_contract_declares_every_topic_and_the_terminal() -> None:
    contract = _contract()
    bus = cast(dict[str, JsonValue], contract["event_bus"])
    subscribed = cast(list[str], bus["subscribe_topics"])
    published = cast(list[str], bus["publish_topics"])
    terminals = cast(dict[str, str], contract["terminal_events"])
    assert subscribed == ["onex.cmd.omnimarket.morning-ground-state-start.v1"]
    assert terminals == {
        "success": "onex.evt.omnimarket.morning-ground-state-completed.v1",
        "failure": "onex.evt.omnimarket.morning-ground-state-failed.v1",
    }
    assert contract["terminal_event"] == terminals["success"]
    runtime_dispatch = cast(dict[str, str], contract["runtime_dispatch"])
    assert runtime_dispatch["command_topic"] == subscribed[0]
    assert contract["externally_consumed_topics"] == [
        terminals["success"],
        terminals["failure"],
    ]
    assert terminals["success"] in published
    assert terminals["failure"] in published
    instance = cast(
        list[dict[str, JsonValue]],
        cast(dict[str, JsonValue], bus["request_response"])["instances"],
    )[0]
    assert instance["request_topic"] in published
    reply = cast(dict[str, str], instance["reply_topics"])
    assert set(reply) == {"completed", "failed"}


@pytest.mark.parametrize("scenario", PARITY["scenarios"], ids=lambda s: str(s["name"]))
def test_original_workflow_decision_parity(scenario: dict[str, JsonValue]) -> None:
    gateway = FixtureGateway(cast(dict[str, JsonValue] | None, scenario["precheck"]))
    handler_type = importlib.import_module(
        f"{PACKAGE}.handlers.handler_morning_ground_state"
    ).HandlerMorningGroundState
    result = asyncio.run(
        handler_type(None, gateway, public_overlay()).handle(
            request(cast(dict[str, JsonValue], scenario["args"]))
        )
    )
    assert (
        result.model_dump(mode="json", exclude={"correlation_id", "tenant_id"})
        == scenario["value"]
    )
    assert gateway.calls == scenario["calls"]
    assert gateway.reconciled == [result.publish]
