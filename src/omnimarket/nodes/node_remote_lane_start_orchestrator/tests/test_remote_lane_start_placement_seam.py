# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane start's placement call meets the placement compute node (OMN-22848).

The seam under proof joins the lane start orchestrator (``node_remote_lane_start_orchestrator``,
step (2) Place: it builds the ``ModelRemoteLanePlacementRequest`` from its pool snapshot and
reads ``outcome``, ``host`` and the ``verdicts`` off the ``ModelRemoteLanePlacementResult``) to
the placement compute node (``node_remote_lane_compute``, whose own topics are declared in
``node_remote_lane_compute/contract.yaml``). Producer side, in the test process: the
orchestrator's own recorded placement request, as it really built it over the scripted
fixtures, is the bus request; a case may carry a ``rename`` mapping that replants one of the
request's keys first. Consumer side: ``ModelRemoteLanePlacementRequest.model_validate`` accepts
the JSON and a fresh ``HandlerRemoteLanePlacement`` judges it; a request it will not validate
is answered with the error the consumer raised, not the exception. The bus carries the JSON of
the model, because the seam is an in-process call whose models are the contract. The as-built
case is pinned green; the planted rename ``engine`` -> ``engine_id`` is the failing control
that must turn the pin red. The in-memory bus leg runs everywhere. The Redpanda leg runs
against the broker the lab names in ``FLOOR_FILL_PARITY_BOOTSTRAP``:

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -k placement_seam -q
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_spi.contracts.services import (
    ModelLaneHostPoolReading,
    ModelLaneHostPoolSnapshot,
)
from pydantic import ValidationError

from omnimarket.models.model_engine_ladder import ModelEngineLadder
from omnimarket.models.model_scenario_row import ModelScenarioRow
from omnimarket.models.model_scenario_run import ModelScenarioRun
from omnimarket.models.remote_lane_placement.model_remote_lane_placement import (
    EnumRemoteLaneOutcome,
    ModelRemoteLaneHostReading,
    ModelRemoteLaneHostVerdict,
    ModelRemoteLanePlacementRequest,
    ModelRemoteLanePlacementResult,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EnumRemoteLaneStartOutcome,
    ModelRemoteLaneStartResult,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_bus import (
    BOOTSTRAP_ENV,
    Bus,
    InmemoryBus,
    RedpandaBus,
    topic_prefix,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    DONE_MESSAGE,
    Rig,
    cells,
    request,
    snapshot,
    stream_result,
)

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

OVERLAY = ModelFactoryBindingsOverlay.model_validate(
    {
        "profiles": [{"id": "inmemory", "baseline": True}, {"id": "redpanda"}],
        "bindings": {"inmemory": {"BUS": "inmemory"}, "redpanda": {"BUS": "redpanda"}},
        "proof_bindings": ["inmemory", "redpanda"],
    }
)

DONE = ModelScriptedTransportScript(
    exit_polls=1, log_lines=("starting", stream_result(DONE_MESSAGE))
)

#: Pinned 2026-11-10 against omnimarket dev: the orchestrator's (2) Place builds the request
#: with exactly these fields; the six it does not forward are the measured gap (the pool
#: snapshot carries launching_venv_hash, the orchestrator does not forward it).
SUPPLIED: set[str] = {
    "engine",
    "pinned_host",
    "repair_target",
    "limited_hosts",
    "codex_limited_hosts",
    "auth_expired_hosts",
    "readings",
    "escalation_reason",
}

UNSUPPLIED: set[str] = {
    "launching_venv_hash",
    "dispatch_venv_required",
    "ladder",
    "lane",
    "stop_gate",
    "required_inputs",
}

#: Pinned: the placement reading's fields the pool's reading does not supply.
POOL_UNSUPPLIED: set[str] = {
    "mem_pressure",
    "mem_psi_avg60",
    "present_inputs",
    "ssh_reach",
    "swap_alarm_open",
    "swap_in_kb_s",
}


class RecordingPlacement(HandlerRemoteLanePlacement):
    """The placement handler of record: every request and result it judges is kept."""

    def __init__(
        self,
        engines: ModelEngineLadder | None = None,
        stop_fsm: ModelFSMSubcontract | None = None,
    ) -> None:
        super().__init__(engines, stop_fsm)
        self.requests: list[ModelRemoteLanePlacementRequest] = []
        self.results: list[ModelRemoteLanePlacementResult] = []

    def handle(
        self, request: ModelRemoteLanePlacementRequest
    ) -> ModelRemoteLanePlacementResult:
        self.requests.append(request)
        result = super().handle(request)
        self.results.append(result)
        return result


def run_recording(
    tmp: Path, pool: ModelLaneHostPoolSnapshot | None = None
) -> tuple[Rig, RecordingPlacement, ModelRemoteLaneStartResult]:
    """One orchestrator run through a recording placement: the request it really built."""
    recorder = RecordingPlacement()
    rig = Rig(tmp, DONE, pool=pool or snapshot(), placement=recorder)
    return rig, recorder, rig.orchestrator.run(request())


def recorded_request(tmp: Path) -> ModelRemoteLanePlacementRequest:
    """The placement request the orchestrator really built on the as-shipped fixtures."""
    _, recorder, _ = run_recording(tmp)
    return recorder.requests[0]


def seam_cases() -> list[tuple[str, dict[str, Any]]]:
    """The two cases: the orchestrator's request as built, and the planted key rename."""
    return [
        ("00:as-built", {}),
        ("01:planted-rename", {"rename": {"engine": "engine_id"}}),
    ]


def producer_request(
    recorded: ModelRemoteLanePlacementRequest, case: dict[str, Any]
) -> dict[str, Any]:
    """The JSON request the producer side sends: the recorded request's JSON, the rename applied."""
    dumped = recorded.model_dump(mode="json")
    for old, new in (case.get("rename") or {}).items():
        dumped[new] = dumped.pop(old)
    return dumped


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """The consumer side: judge the placement request the JSON it carries against the model."""
    try:
        recorded = ModelRemoteLanePlacementRequest.model_validate(payload["request"])
    except ValidationError as refused:
        return {"id": payload["id"], "error": refused.__class__.__name__}
    result = HandlerRemoteLanePlacement().handle(recorded)
    return {"id": payload["id"], "output": result.model_dump(mode="json")}


def projected(reply: dict[str, Any]) -> dict[str, Any]:
    """The projection the rows carry: the output, or the error the consumer reported."""
    return {"error": reply["error"]} if "output" not in reply else reply["output"]


def in_process(recorded: ModelRemoteLanePlacementRequest) -> dict[str, str]:
    return {
        name: json.dumps(
            projected(
                decide({"id": name, "request": producer_request(recorded, case)})
            ),
            sort_keys=True,
        )
        for name, case in seam_cases()
    }


async def run_over(
    bus: Bus, profile: str, binding: str, recorded: ModelRemoteLanePlacementRequest
) -> ModelScenarioRun:
    await bus.start()
    try:
        rows = []
        for name, case in seam_cases():
            reply = await bus.call(
                {"id": uuid.uuid4().hex, "request": producer_request(recorded, case)}
            )
            rows.append(
                ModelScenarioRow(
                    key=name, content=json.dumps(projected(reply), sort_keys=True)
                )
            )
    finally:
        await bus.stop()
    return ModelScenarioRun(
        profile_id=profile,
        binding=binding,
        proof_eligible=True,
        verdicts=("PASS",),
        rows=tuple(rows),
        counts={"decisions": len(rows)},
    )


def make_bus(binding: str) -> Bus:
    prefix = topic_prefix("lane-start-placement-seam")
    if binding == "inmemory":
        return InmemoryBus(decide, prefix)
    return RedpandaBus(decide, prefix, os.environ[BOOTSTRAP_ENV])


def seam_run(
    recorded: ModelRemoteLanePlacementRequest, *, profiles: tuple[str, ...]
) -> tuple[ModelScenarioRun, ...]:
    chosen = ModelFactoryBindingsOverlay.model_validate(
        {
            "profiles": [p.model_dump() for p in OVERLAY.profiles if p.id in profiles],
            "bindings": {p: OVERLAY.bindings[p] for p in profiles},
            "proof_bindings": list(OVERLAY.proof_bindings),
        }
    )
    return tuple(
        asyncio.run(
            run_over(
                make_bus(chosen.binding_id(profile.id, "BUS")),
                profile.id,
                chosen.binding_id(profile.id, "BUS"),
                recorded,
            )
        )
        for profile in chosen.profiles
    )


def test_placement_seam_request_fields_supplied_are_the_pinned_set(
    tmp_path: Path,
) -> None:
    _, recorder, _ = run_recording(tmp_path)
    assert recorder.requests[0].model_fields_set == SUPPLIED
    assert set(ModelRemoteLanePlacementRequest.model_fields) - SUPPLIED == UNSUPPLIED


def test_placement_seam_pool_reading_fields_all_exist_on_the_placement_reading() -> (
    None
):
    pool_fields = set(ModelLaneHostPoolReading.model_fields)
    placement_fields = set(ModelRemoteLaneHostReading.model_fields)
    assert pool_fields - {"target"} <= placement_fields
    assert placement_fields - pool_fields == POOL_UNSUPPLIED


def test_placement_seam_result_fields_the_orchestrator_reads_exist() -> None:
    assert {"outcome", "host", "verdicts"} <= set(
        ModelRemoteLanePlacementResult.model_fields
    )
    assert {"host", "reason"} <= set(ModelRemoteLaneHostVerdict.model_fields)
    assert {"ADMITTED", "NO_HOST"} <= {member.value for member in EnumRemoteLaneOutcome}


def test_placement_seam_admitted_decision_names_the_host_the_orchestrator_claims(
    tmp_path: Path,
) -> None:
    rig, recorder, _ = run_recording(tmp_path, pool=snapshot())
    result = recorder.results[0]
    assert result.outcome is EnumRemoteLaneOutcome.ADMITTED
    assert result.host == "h106"
    [claim] = rig.ledger_rows("CLAIM")
    assert cells(claim)["host"] == result.host


def test_placement_seam_no_host_decision_is_surfaced_with_the_verdict_reasons(
    tmp_path: Path,
) -> None:
    rig, recorder, result = run_recording(tmp_path, pool=snapshot(slots=0))
    assert result.outcome is EnumRemoteLaneStartOutcome.NO_HOST
    assert rig.ledger_rows() == []
    assert rig.host_calls() == []
    reason = recorder.results[0].verdicts[0].reason
    assert any(
        line.startswith("REMOTE-LANE-NO-HOST") and reason in line
        for line in result.lines
    )


def test_placement_seam_as_built_projection_is_admitted_on_the_decided_host(
    tmp_path: Path,
) -> None:
    rows = {k: json.loads(v) for k, v in in_process(recorded_request(tmp_path)).items()}
    assert rows["00:as-built"]["outcome"] == EnumRemoteLaneOutcome.ADMITTED.value
    assert rows["00:as-built"]["host"] == "h106"


def test_placement_seam_planted_rename_control_turns_the_pin_red(
    tmp_path: Path,
) -> None:
    """Failing control: a renamed key fails the consumer's validation while 00 stays green."""
    rows = {k: json.loads(v) for k, v in in_process(recorded_request(tmp_path)).items()}
    assert rows["00:as-built"]["outcome"] == EnumRemoteLaneOutcome.ADMITTED.value
    planted = rows["01:planted-rename"]
    assert "error" in planted
    assert planted != rows["00:as-built"]


def test_placement_seam_in_memory_bus_projection_matches_the_in_process_one(
    tmp_path: Path,
) -> None:
    recorded = recorded_request(tmp_path)
    [run] = seam_run(recorded, profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process(recorded)


@pytest.mark.kafka
@pytest.mark.integration
def test_placement_seam_redpanda_bus_projection_matches_the_in_memory_one(
    tmp_path: Path,
) -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    recorded = recorded_request(tmp_path)
    runs = seam_run(recorded, profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process(recorded)
    assert len(runs[1].rows) == len(seam_cases())
