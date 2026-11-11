# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host-call envelope meets the transport protocol it routes to (OMN-23528).

The seam under proof joins the lane start's host-call envelope (``EnumLaneHostOperation``,
``ModelRemoteLaneHostCall`` and its result, in omnimarket) to omnibase_spi's
``ProtocolLaneHostTransport`` and the two transports that implement it (the ssh transport and the scripted
one). The effect handler routes each operation to the transport method of the same name, so the two sides
must agree on every method. Producer side, in the test process: a request naming the planted changes (an
operation renamed, a method added to the protocol) goes onto the bus under test as JSON. Consumer side,
``decide`` reads both sides from their source and answers a projection: the operations with no protocol
method, the protocol methods with no operation, the methods whose request type differs from the envelope's
field, the operations that fail when routed to the scripted transport, and the methods the two transports
declare with different parameters. The as-shipped case is pinned all empty; the two planted cases are the
failing controls that must turn the pin red. The in-memory bus leg runs everywhere. The Redpanda leg runs
against the broker the lab names in ``FLOOR_FILL_PARITY_BOOTSTRAP``:

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -k host_call_seam -q
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import typing
import uuid
from typing import Any

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostRef,
    ModelLaneHostStepRequest,
    ModelLanePrepareWorktreeRequest,
    ModelLaneReadEngineExitRequest,
    ModelLaneReadLogTailRequest,
    ModelLaneShipPluginTreesRequest,
    ModelLaneStageInputRequest,
    ModelLaneStartEngineRequest,
    ModelLaneStopEngineRequest,
)
from omnibase_spi.protocols.lane import ProtocolLaneHostTransport
from pydantic import BaseModel

from omnimarket.models.remote_lane_host_call.model_remote_lane_host_call import (
    REQUEST_FIELD,
    RESULT_FIELD,
    EnumLaneHostOperation,
    ModelRemoteLaneHostCall,
    ModelRemoteLaneHostCallResult,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
    ScriptedLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    SshLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.handlers.handler_remote_lane_start_effect import (
    HandlerRemoteLaneStartEffect,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_bus import (
    BOOTSTRAP_ENV,
    Bus,
    InmemoryBus,
    RedpandaBus,
    topic_prefix,
)

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

#: Pinned 2026-11-10 against the omnibase-spi pin: the envelope and the protocol agree on every method.
EXPECTED_AS_SHIPPED: dict[str, list[str]] = {
    "operations_without_a_method": [],
    "methods_without_an_operation": [],
    "request_types_differ": [],
    "operations_that_fail_when_routed": [],
    "transports_disagree": [],
}

HOST = ModelLaneHostRef(name="h106", target="h106")
COMMON: dict[str, Any] = {"host": HOST, "lane": "seam-lane", "run_id": "seam-run"}
MINIMAL_REQUESTS: dict[str, BaseModel] = {
    "prepare": ModelLanePrepareWorktreeRequest(
        **COMMON,
        repo="omnimarket",
        owner="OmniNode-ai",
        sha="a" * 40,
        registry_root_candidates=("/registry",),
        timeout_seconds=5,
    ),
    "start": ModelLaneStartEngineRequest(
        **COMMON,
        workdir="/runs/seam-run",
        engine_argv=("true",),
        environment=(),
        timeout_seconds=5,
    ),
    "engine_exit": ModelLaneReadEngineExitRequest(host=HOST, run_id="seam-run"),
    "stop": ModelLaneStopEngineRequest(host=HOST, run_id="seam-run", grace_seconds=1),
    "log_tail": ModelLaneReadLogTailRequest(host=HOST, run_id="seam-run", offset=0),
    "step": ModelLaneHostStepRequest(**COMMON, timeout_seconds=5),
    "stage": ModelLaneStageInputRequest(
        **COMMON, name="x.txt", content="x", timeout_seconds=5
    ),
    "ship": ModelLaneShipPluginTreesRequest(**COMMON, trees=(), timeout_seconds=5),
}


def protocol_methods() -> list[str]:
    """The public calls of ProtocolLaneHostTransport, read from the protocol class."""
    return sorted(
        name
        for name, member in vars(ProtocolLaneHostTransport).items()
        if callable(member) and not name.startswith("_")
    )


def field_type_name(model: type[BaseModel], field: str) -> str:
    """The class name the envelope's field accepts, with ``| None`` dropped."""
    annotation = model.model_fields[field].annotation
    options = [a for a in typing.get_args(annotation) if a is not type(None)]
    return options[0].__name__ if options else getattr(annotation, "__name__", "")


def request_annotation_name(method: str) -> str:
    parameter = inspect.signature(getattr(ProtocolLaneHostTransport, method)).parameters
    return str(parameter["request"].annotation).strip("'\"")


def parameter_names(owner: type, method: str) -> list[str]:
    return list(inspect.signature(getattr(owner, method)).parameters)


def routed_failures(operations: list[EnumLaneHostOperation]) -> list[str]:
    """The operations that do not answer when routed through the effect to the scripted transport."""
    effect = HandlerRemoteLaneStartEffect(
        ScriptedLaneHostTransport(ModelScriptedTransportScript())
    )
    failed: list[str] = []
    for op in operations:
        try:
            call = ModelRemoteLaneHostCall(
                operation=op, **{REQUEST_FIELD[op]: MINIMAL_REQUESTS[REQUEST_FIELD[op]]}
            )
            answer = effect.handle(call)
            if getattr(answer, RESULT_FIELD[op]) is None:
                failed.append(op.value)
        except (ValueError, AttributeError, TypeError):
            failed.append(op.value)
    return sorted(failed)


def projection(
    rename: dict[str, str] | None = None, extra_methods: list[str] | None = None
) -> dict[str, list[str]]:
    """Both sides read from source; the planted changes edit what the envelope or the protocol appears to hold."""
    renamed = rename or {}
    methods = sorted([*protocol_methods(), *(extra_methods or [])])
    operations = [renamed.get(op.value, op.value) for op in EnumLaneHostOperation]
    differing = [
        op.value
        for op in EnumLaneHostOperation
        if op.value in protocol_methods()
        and field_type_name(ModelRemoteLaneHostCall, REQUEST_FIELD[op])
        != request_annotation_name(op.value)
    ]
    disagree = [
        m
        for m in protocol_methods()
        if parameter_names(SshLaneHostTransport, m)
        != parameter_names(ScriptedLaneHostTransport, m)
    ]
    return {
        "operations_without_a_method": sorted(set(operations) - set(methods)),
        "methods_without_an_operation": sorted(set(methods) - set(operations)),
        "request_types_differ": sorted(differing),
        "operations_that_fail_when_routed": routed_failures(
            list(EnumLaneHostOperation)
        ),
        "transports_disagree": sorted(disagree),
    }


def seam_cases() -> list[tuple[str, dict[str, Any]]]:
    """The three cases: as shipped, a planted operation rename, a planted new protocol method."""
    return [
        ("00:as-shipped", {}),
        ("01:planted-op-rename", {"rename": {"stage_file": "stage_files"}}),
        ("02:planted-new-protocol-method", {"extra_methods": ["warm_up_host"]}),
    ]


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """The consumer side: judge the envelope against the protocol and the transports."""
    request = payload["request"]
    return {
        "id": payload["id"],
        "output": projection(request.get("rename"), request.get("extra_methods")),
    }


def in_process() -> dict[str, str]:
    return {
        name: json.dumps(
            decide({"id": name, "request": case})["output"], sort_keys=True
        )
        for name, case in seam_cases()
    }


async def run_over(bus: Bus) -> dict[str, str]:
    await bus.start()
    try:
        rows = {}
        for name, case in seam_cases():
            reply = await bus.call({"id": uuid.uuid4().hex, "request": case})
            rows[name] = json.dumps(reply["output"], sort_keys=True)
    finally:
        await bus.stop()
    return rows


def test_host_call_seam_as_shipped_projection_matches_the_pin_in_process() -> None:
    rows = {k: json.loads(v) for k, v in in_process().items()}
    assert rows["00:as-shipped"] == EXPECTED_AS_SHIPPED


def test_host_call_seam_every_operation_has_a_request_and_a_result_field() -> None:
    assert set(REQUEST_FIELD) == set(EnumLaneHostOperation) == set(RESULT_FIELD)
    assert all(
        name in ModelRemoteLaneHostCall.model_fields for name in REQUEST_FIELD.values()
    )
    assert all(
        name in ModelRemoteLaneHostCallResult.model_fields
        for name in RESULT_FIELD.values()
    )
    assert len(protocol_methods()) == len(EnumLaneHostOperation)


def test_host_call_seam_planted_controls_turn_the_pin_red() -> None:
    """Failing control: a renamed operation and a new protocol method each turn the pin red while 00 stays green."""
    rows = {k: json.loads(v) for k, v in in_process().items()}
    assert rows["00:as-shipped"] == EXPECTED_AS_SHIPPED
    renamed = rows["01:planted-op-rename"]
    assert renamed["operations_without_a_method"] == ["stage_files"]
    assert renamed["methods_without_an_operation"] == ["stage_file"]
    added = rows["02:planted-new-protocol-method"]
    assert added["methods_without_an_operation"] == ["warm_up_host"]
    assert added["operations_without_a_method"] == []


def test_host_call_seam_in_memory_bus_projection_matches_the_in_process_one() -> None:
    bus = InmemoryBus(decide, topic_prefix("host-call-seam"))
    assert asyncio.run(run_over(bus)) == in_process()


@pytest.mark.kafka
@pytest.mark.integration
def test_host_call_seam_redpanda_bus_projection_matches_the_in_process_one() -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    bus = RedpandaBus(decide, topic_prefix("host-call-seam"), os.environ[BOOTSTRAP_ENV])
    assert asyncio.run(run_over(bus)) == in_process()
