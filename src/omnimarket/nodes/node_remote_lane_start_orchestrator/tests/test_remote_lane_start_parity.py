# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane start decides the same on the in-memory bus and on a Redpanda bus (OMN-22848, plan T15 AC7).

Four scenarios (placed and the engine ends declaring a PR, no host, CLAIM refused, engine fails)
each go as a JSON message on the bus under test; the consumer runs the orchestrator over the
scripted transport and a ledger fixture file and publishes its decision projection back: the
outcome, the exit code, the printed lines, the ordered host and ledger calls and the CLAIM and
TERMINAL cells (time and run id excluded). ``compare_runs`` judges the in-memory run against the
Redpanda run.

The in-memory leg runs everywhere. The Redpanda leg runs against the broker the lab names in
``FLOOR_FILL_PARITY_BOOTSTRAP`` (a plaintext lab broker the caller started for the proof):

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -k remote_lane_start -q
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_scenario_row import ModelScenarioRow
from omnimarket.models.model_scenario_run import ModelScenarioRun
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
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


def golden_scenario() -> list[tuple[str, dict[str, Any]]]:
    done = {"exit_polls": 1, "log_lines": ["starting", stream_result(DONE_MESSAGE)]}
    return [
        ("00:placed", {"script": done, "slots": 2, "refuse": False}),
        ("01:no-host", {"script": done, "slots": 0, "refuse": False}),
        ("02:claim-refused", {"script": done, "slots": 2, "refuse": True}),
        (
            "03:engine-fails",
            {
                "script": {"exit_code": 1, "log_lines": ["boom"]},
                "slots": 2,
                "refuse": False,
            },
        ),
    ]


def project(scenario: dict[str, Any]) -> dict[str, Any]:
    """Run one scenario through the orchestrator and return its decision projection."""
    with tempfile.TemporaryDirectory() as tmp:
        rig = Rig(
            Path(tmp),
            ModelScriptedTransportScript.model_validate(scenario["script"]),
            pool=snapshot(slots=scenario["slots"]),
            refuse_ledger=scenario["refuse"],
        )
        result = rig.orchestrator.run(request())
        rows = {
            kind: [cells(r) for r in rig.ledger_rows(kind)]
            for kind in ("CLAIM", "TERMINAL", "STATUS")
        }
        return {
            "outcome": result.outcome.value,
            "exit_code": result.exit_code,
            "lines": list(result.lines),
            "calls": rig.calls,
            "rows": rows,
            "receipt_state": result.receipt.state,
        }


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    return {"id": payload["id"], "output": project(payload["request"])}


def in_process() -> dict[str, str]:
    return {
        name: json.dumps(project(req), sort_keys=True)
        for name, req in golden_scenario()
    }


async def run_over(bus: Bus, profile: str, binding: str) -> ModelScenarioRun:
    await bus.start()
    try:
        rows = []
        for name, req in golden_scenario():
            reply = await bus.call({"id": uuid.uuid4().hex, "request": req})
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
    prefix = topic_prefix("remote-lane-start-parity")
    if binding == "inmemory":
        return InmemoryBus(decide, prefix)
    return RedpandaBus(decide, prefix, os.environ[BOOTSTRAP_ENV])


def parity_run(*, profiles: tuple[str, ...]) -> tuple[ModelScenarioRun, ...]:
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
            )
        )
        for profile in chosen.profiles
    )


def test_remote_lane_start_parity_the_scenario_decides_what_the_sequence_says() -> None:
    rows = {k: json.loads(v) for k, v in in_process().items()}
    assert rows["00:placed"]["outcome"] == "ended"
    assert rows["00:placed"]["exit_code"] == 0
    assert rows["00:placed"]["rows"]["TERMINAL"][0]["outcome"] == "handed-off"
    assert rows["01:no-host"]["outcome"] == "no-host"
    assert rows["01:no-host"]["calls"] == []
    assert rows["02:claim-refused"]["exit_code"] == 70
    assert not [c for c in rows["02:claim-refused"]["calls"] if c.startswith("host:")]
    assert rows["03:engine-fails"]["rows"]["TERMINAL"][0]["outcome"] == "failed"


def test_remote_lane_start_parity_in_memory_bus_matches_the_in_process_decisions() -> (
    None
):
    [run] = parity_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()


@pytest.mark.kafka
@pytest.mark.integration
def test_remote_lane_start_parity_redpanda_overlay_matches_the_in_memory_overlay() -> (
    None
):
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = parity_run(profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process()
    assert len(runs[1].rows) == len(golden_scenario())
