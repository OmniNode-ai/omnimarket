# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves, its handler runs a typed request to a typed terminal.

Decision parity: six transcripts recorded from the original workflow (fresh run,
already delivered, peer owned, unavailable precheck, force, dry run) produce the
same terminal, the same phase order and the same model/effort/schema routing.
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

from omnimarket.nodes.node_morning_friction_sweep_orchestrator.models import (
    ModelMorningFrictionSweepRequest,
    ModelMorningFrictionSweepResult,
)

from .helpers import NODE, FixtureGateway, public_overlay, request

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "decision_parity.json").read_text()
)["cases"]
PACKAGE = f"omnimarket.nodes.{NODE}"
LABELS = [
    "friction-precheck",
    "friction-scan",
    "friction-source-linear",
    "friction-source-checkpoints",
    "friction-source-ci",
    "friction-source-guards",
    "friction-synthesize",
    "friction-adjudicate",
    "friction-report",
]


def _contract() -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        yaml.safe_load(files(PACKAGE).joinpath("contract.yaml").read_text()),
    )


def _handler_type() -> type:
    return cast(
        type,
        importlib.import_module(
            f"{PACKAGE}.handlers.handler_morning_friction_sweep"
        ).HandlerMorningFrictionSweep,
    )


def test_golden_chain_morning_friction_sweep_orchestrator() -> None:
    contract = _contract()
    assert contract["name"] == NODE
    assert contract["node_type"] == "orchestrator"
    routing = cast(dict[str, JsonValue], contract["handler_routing"])
    assert routing["routing_strategy"] == "operation_match"
    entries = cast(list[dict[str, JsonValue]], routing["handlers"])
    assert [entry["operation"] for entry in entries] == ["morning_friction_sweep"]
    entry = entries[0]
    handler_ref = cast(dict[str, str], entry["handler"])
    handler_type = getattr(
        importlib.import_module(handler_ref["module"]), handler_ref["name"]
    )
    input_module, _, input_name = str(entry["input_model"]).rpartition(".")
    output_module, _, output_name = str(entry["output_model"]).rpartition(".")
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)
    assert request_type is ModelMorningFrictionSweepRequest
    assert result_type is ModelMorningFrictionSweepResult
    # Definition-B: one typed request in, one typed result out.
    assert inspect.iscoroutinefunction(handler_type.handle)
    assert list(inspect.signature(handler_type.handle).parameters) == [
        "self",
        "request",
    ]
    gateway = FixtureGateway()
    result = asyncio.run(
        handler_type(None, gateway, public_overlay()).handle(request())
    )
    assert isinstance(result, result_type)
    assert [call["label"] for call in gateway.calls] == LABELS
    assert result.agents == 9
    assert result.short_circuited is None


def test_contract_declares_every_topic_and_the_terminal() -> None:
    contract = _contract()
    bus = cast(dict[str, JsonValue], contract["event_bus"])
    subscribed = cast(list[str], bus["subscribe_topics"])
    published = cast(list[str], bus["publish_topics"])
    terminals = cast(dict[str, str], contract["terminal_events"])
    assert subscribed == ["onex.cmd.omnimarket.morning-friction-sweep-start.v1"]
    assert terminals == {
        "success": "onex.evt.omnimarket.morning-friction-sweep-completed.v1",
        "failure": "onex.evt.omnimarket.morning-friction-sweep-failed.v1",
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
    assert set(cast(dict[str, str], instance["reply_topics"])) == {
        "completed",
        "failed",
    }


@pytest.mark.parametrize("case", CASES, ids=lambda c: str(c["name"]))
def test_original_workflow_decision_parity(case: dict[str, JsonValue]) -> None:
    gateway = FixtureGateway(cast(dict[str, JsonValue], case["responses"]))
    result = asyncio.run(
        _handler_type()(None, gateway, public_overlay()).handle(
            request(cast(dict[str, JsonValue], case["args"]))
        )
    )
    dumped = result.model_dump(
        mode="json", exclude={"correlation_id", "tenant_id"}, exclude_unset=True
    )
    dumped["dryRun"] = dumped.pop("dry_run")
    assert dumped == case["value"]
    assert gateway.calls == case["calls"]
    assert gateway.peak == (5 if result.short_circuited is None else 0)


@pytest.mark.parametrize("verdict", ["already-delivered", "peer-owned"])
def test_a_same_date_precheck_verdict_spawns_zero_expensive_agents(
    verdict: str,
) -> None:
    case = next(c for c in CASES if c.get("precheckVerdict") == verdict)
    gateway = FixtureGateway(cast(dict[str, JsonValue], case["responses"]))
    result = asyncio.run(
        _handler_type()(None, gateway, public_overlay()).handle(
            request(cast(dict[str, JsonValue], case["args"]))
        )
    )
    assert result.short_circuited == verdict
    assert result.expensive_agents_spawned == 0
    assert [c["label"] for c in gateway.calls] == ["friction-precheck"]
