# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start orchestrator's placement request meets the placement compute node (OMN-23578).

The seam joins ``RemoteLaneStartOrchestrator`` (step "Place", which builds a
``ModelRemoteLanePlacementRequest`` from the start request and the host pool snapshot) to
``HandlerRemoteLanePlacement`` (``node_remote_lane_compute``), whose ``outcome``, ``host`` and
``verdicts`` the orchestrator reads back. Producer side: a recording placement captures the
request the orchestrator really builds. Consumer side: the real placement handler decides it.
Pins name the fields the orchestrator supplies, the ones it leaves unsupplied (the measured
gap: the pool snapshot carries ``launching_venv_hash`` and the start does not pass it on) and
the result fields it reads. The planted rename ``engine`` -> ``engine_id`` is the failing
control: the placement model must refuse it. The in-memory bus leg runs everywhere; the
Redpanda leg runs against the broker named in ``FLOOR_FILL_PARITY_BOOTSTRAP``:

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
from omnibase_spi.contracts.services import ModelLaneHostPoolReading
from pydantic import ValidationError

from omnimarket.models.model_scenario_row import ModelScenarioRow
from omnimarket.models.model_scenario_run import ModelScenarioRun
from omnimarket.models.remote_lane_placement.model_remote_lane_placement import (
    ModelRemoteLaneHostReading,
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
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_bus import (
    BOOTSTRAP_ENV,
    Bus,
    InmemoryBus,
    RedpandaBus,
    topic_prefix,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    Rig,
    request,
)

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

OVERLAY = ModelFactoryBindingsOverlay.model_validate(
    {
        "profiles": [{"id": "inmemory", "baseline": True}, {"id": "redpanda"}],
        "bindings": {"inmemory": {"BUS": "inmemory"}, "redpanda": {"BUS": "redpanda"}},
        "proof_bindings": ["inmemory", "redpanda"],
    }
)

#: Pinned 2026-11-10 against omnimarket dev: the fields the orchestrator sets on the request.
SUPPLIED = frozenset(
    {
        "engine",
        "pinned_host",
        "repair_target",
        "limited_hosts",
        "codex_limited_hosts",
        "auth_expired_hosts",
        "readings",
        "escalation_reason",
    }
)
#: The measured gap: request fields the orchestrator leaves at their defaults.
UNSUPPLIED = frozenset(
    {
        "launching_venv_hash",
        "dispatch_venv_required",
        "ladder",
        "lane",
        "stop_gate",
        "required_inputs",
    }
)
#: The result fields the orchestrator reads.
READ_BACK = frozenset({"outcome", "host", "verdicts"})
#: Reading fields only the placement model carries (the pool reading has no source for them).
PLACEMENT_ONLY_READING_FIELDS = frozenset(
    {
        "mem_pressure",
        "mem_psi_avg60",
        "present_inputs",
        "ssh_reach",
        "swap_alarm_open",
        "swap_in_kb_s",
    }
)


class RecordingPlacement(HandlerRemoteLanePlacement):
    """The real placement handler, remembering the request the orchestrator built."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[ModelRemoteLanePlacementRequest] = []

    def handle(self, request: ModelRemoteLanePlacementRequest) -> Any:
        self.seen.append(request)
        return super().handle(request)


def recorded_request(tmp: Path) -> ModelRemoteLanePlacementRequest:
    recorder = RecordingPlacement()
    rig = Rig(tmp, placement=recorder)
    rig.orchestrator.run(request())
    assert len(recorder.seen) == 1
    return recorder.seen[0]


def seam_cases(tmp: Path) -> list[tuple[str, dict[str, Any]]]:
    """The as-shipped request and the planted rename of its engine key."""
    shipped = recorded_request(tmp).model_dump(mode="json")
    planted = {("engine_id" if k == "engine" else k): v for k, v in shipped.items()}
    return [("00:as-shipped", shipped), ("01:planted-rename", planted)]


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """The consumer side: the real placement handler over the request, or its refusal."""
    try:
        req = ModelRemoteLanePlacementRequest.model_validate(payload["request"])
    except ValidationError as err:
        return {
            "id": payload["id"],
            "output": {"rejected": str(err.errors()[0]["loc"][0])},
        }
    got = HandlerRemoteLanePlacement().handle(req)
    return {
        "id": payload["id"],
        "output": {
            "outcome": got.outcome.value,
            "host": got.host,
            "verdicts": [[v.host, v.reason] for v in got.verdicts],
        },
    }


def in_process(tmp: Path) -> dict[str, str]:
    return {
        name: json.dumps(
            decide({"id": name, "request": body})["output"], sort_keys=True
        )
        for name, body in seam_cases(tmp)
    }


async def run_over(bus: Bus, profile: str, binding: str, tmp: Path) -> ModelScenarioRun:
    await bus.start()
    try:
        rows = []
        for name, body in seam_cases(tmp):
            reply = await bus.call({"id": uuid.uuid4().hex, "request": body})
            rows.append(
                ModelScenarioRow(
                    key=name, content=json.dumps(reply["output"], sort_keys=True)
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


def seam_run(tmp: Path, *, profiles: tuple[str, ...]) -> tuple[ModelScenarioRun, ...]:
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
                tmp,
            )
        )
        for profile in chosen.profiles
    )


def test_placement_seam_supplied_and_unsupplied_fields_cover_the_request(
    tmp_path: Path,
) -> None:
    assert set(ModelRemoteLanePlacementRequest.model_fields) == SUPPLIED | UNSUPPLIED
    assert not SUPPLIED & UNSUPPLIED
    assert recorded_request(tmp_path).model_fields_set == SUPPLIED


def test_placement_seam_pool_reading_fields_are_all_placement_reading_fields() -> None:
    pool = set(ModelLaneHostPoolReading.model_fields) - {"target"}
    placement = set(ModelRemoteLaneHostReading.model_fields)
    assert pool <= placement
    assert placement - pool == PLACEMENT_ONLY_READING_FIELDS


def test_placement_seam_the_fields_the_orchestrator_reads_back_exist() -> None:
    assert set(ModelRemoteLanePlacementResult.model_fields) >= READ_BACK


def test_placement_seam_planted_rename_control_turns_the_pin_red(
    tmp_path: Path,
) -> None:
    """Failing control: the renamed engine key is refused while 00 stays admitted."""
    rows = {k: json.loads(v) for k, v in in_process(tmp_path).items()}
    assert rows["00:as-shipped"]["outcome"] == "ADMITTED"
    assert rows["00:as-shipped"]["host"] == "h106"
    assert rows["01:planted-rename"] == {"rejected": "engine"}


def test_placement_seam_in_memory_bus_projection_matches_the_in_process_one(
    tmp_path: Path,
) -> None:
    [run] = seam_run(tmp_path, profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process(tmp_path)


@pytest.mark.kafka
@pytest.mark.integration
def test_placement_seam_redpanda_bus_projection_matches_the_in_memory_one(
    tmp_path: Path,
) -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = seam_run(tmp_path, profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process(tmp_path)
    assert len(runs[1].rows) == 2
