# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start orchestrator's observe event meets the host table's placement (OMN-23663).

Two seams join the ``RemoteLaneStartOrchestrator`` (``start_mode: observe``) to the readers that
consume its output. SEAM A: the orchestrator places the lane in observe mode and publishes the
``ModelRemoteLaneStartObserved`` event; the consumer validates it and reports the fields it reads.
SEAM B: the lab host table's unavailable hosts meet the start's placement; the consumer reports
which hosts the table keeps out. The planted renames are the failing controls: a renamed key on
either side must be refused. The in-memory bus leg runs everywhere; the Redpanda leg runs over the
declared bus lane named in ``LANE_START_SEAM_LANE`` (workspace root in ``OMNIBASE_PATH``):

    LANE_START_SEAM_LANE=dev OMNIBASE_PATH=<workspace root> uv run pytest -m "floor_fill_parity and kafka" -k observe_seam -q
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.models.model_scenario_row import ModelScenarioRow
from omnimarket.models.model_scenario_run import ModelScenarioRun
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.adapter_host_availability import (
    HostTableUnreadableError,
    lab_host_table_unavailable,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    ModelRemoteLaneStartObserved,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_bus import (
    LANE_ENV,
    ROOT_ENV,
    Bus,
    InmemoryBus,
    LaneBus,
    topic_prefix,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    Rig,
    request,
    settings,
)

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

# The real-broker leg's topics (SEAM_NAME + ".req" and ".res") and consumer groups (SEAM_GROUP_PREFIX
# + a run id) are fixed names, so the dev principal's broker grants can name them; an unnamed topic
# is refused by the broker's ACLs (TopicAuthorizationFailedError).
SEAM_NAME = "lane-start-observe-seam"
SEAM_GROUP_PREFIX = "lane-start-observe-seam-"

OVERLAY = ModelFactoryBindingsOverlay.model_validate(
    {
        "profiles": [{"id": "inmemory", "baseline": True}, {"id": "redpanda"}],
        "bindings": {"inmemory": {"BUS": "inmemory"}, "redpanda": {"BUS": "redpanda"}},
        "proof_bindings": ["inmemory", "redpanda"],
    }
)

#: Pinned against the as-shipped observe event: the fields the consumer reads and their values.
EXPECTED_EVENT = {
    "host": "h106",
    "lane": "floor-alpha-a1",
    "argv_has_brief_reference": True,
    "fields": [
        "account_label",
        "account_label_source",
        "attempt",
        "brief_bytes",
        "brief_sha256",
        "claim_cells",
        "engine",
        "engine_argv",
        "engine_environment_names",
        "host",
        "host_local",
        "host_target",
        "key",
        "lane",
        "model",
        "observed_at",
        "refused_hosts",
        "run_id",
        "ticket",
        "workdir",
    ],
}

#: The host table the SEAM B producer writes; h202 is out for repair.
HOST_TABLE: dict[str, Any] = {
    "hosts": [
        {"name": "h106", "target": "h106"},
        {
            "name": "h202",
            "target": "h202",
            "available": False,
            "unavailable_reason": "down for repair",
        },
    ]
}

#: Pinned against the host table: the hosts the table keeps out of placement.
EXPECTED_TABLE = {"unavailable": {"h202": "down for repair"}}


def observe_event(tmp: Path) -> dict[str, Any]:
    """The observe event the orchestrator publishes, as JSON (the bus request must be JSON)."""
    rig = Rig(tmp, cfg=settings(start_mode="observe"))
    result = rig.orchestrator.run(request())
    assert result.observed is not None
    return result.observed.model_dump(mode="json")


def seam_cases(tmp: Path) -> list[tuple[str, dict[str, Any]]]:
    """The four cases: the observe event, its planted rename, the host table, its planted rename."""
    event = observe_event(tmp)
    planted_event = {("placed_host" if k == "host" else k): v for k, v in event.items()}
    table_path = tmp / "lab_hosts.json"
    table_path.write_text(json.dumps(HOST_TABLE), encoding="utf-8")
    planted_table = {
        "hosts": [
            row
            if row["name"] != "h202"
            else {("in_service" if k == "available" else k): v for k, v in row.items()}
            for row in HOST_TABLE["hosts"]
        ]
    }
    planted_path = tmp / "lab_hosts_planted.json"
    planted_path.write_text(json.dumps(planted_table), encoding="utf-8")
    return [
        ("00:observe-event", {"kind": "observe", "event": event}),
        ("01:planted-rename", {"kind": "observe", "event": planted_event}),
        ("02:host-table", {"kind": "table", "path": str(table_path)}),
        ("03:planted-rename", {"kind": "table", "path": str(planted_path)}),
    ]


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """The consumer side: the observe event or the host table, or its refusal."""
    body = payload["request"]
    if body["kind"] == "observe":
        try:
            model = ModelRemoteLaneStartObserved.model_validate(body["event"])
        except ValidationError as err:
            return {
                "id": payload["id"],
                "output": {"rejected": str(err.errors()[0]["loc"][0])},
            }
        return {
            "id": payload["id"],
            "output": {
                "host": model.host,
                "lane": model.lane,
                "argv_has_brief_reference": any(
                    "<brief sha256:" in arg for arg in model.engine_argv
                ),
                "fields": sorted(model.model_dump(mode="json")),
            },
        }
    try:
        unavailable = lab_host_table_unavailable({"ONEX_LAB_RUN_HOSTS": body["path"]})
    except HostTableUnreadableError:
        return {"id": payload["id"], "output": {"rejected": "table"}}
    return {"id": payload["id"], "output": {"unavailable": dict(unavailable)}}


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
    if binding == "inmemory":
        return InmemoryBus(decide, topic_prefix(SEAM_NAME))
    return LaneBus(
        decide,
        SEAM_NAME,
        os.environ[LANE_ENV],
        Path(os.environ[ROOT_ENV]),
        group=f"{SEAM_GROUP_PREFIX}{uuid.uuid4().hex}",
    )


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


def test_observe_seam_the_observe_event_matches_the_pin(tmp_path: Path) -> None:
    """The as-shipped observe event validates and the consumer reads the pinned fields."""
    rows = {k: json.loads(v) for k, v in in_process(tmp_path).items()}
    assert rows["00:observe-event"] == EXPECTED_EVENT


def test_observe_seam_planted_rename_control_turns_the_pin_red(tmp_path: Path) -> None:
    """Failing control: the renamed host key is refused while 00 stays admitted."""
    rows = {k: json.loads(v) for k, v in in_process(tmp_path).items()}
    assert rows["01:planted-rename"] == {"rejected": "host"}
    assert rows["01:planted-rename"] != EXPECTED_EVENT


def test_observe_seam_the_host_table_reports_the_unavailable_host(
    tmp_path: Path,
) -> None:
    """The host table's unavailable host meets the start's placement."""
    rows = {k: json.loads(v) for k, v in in_process(tmp_path).items()}
    assert rows["02:host-table"] == EXPECTED_TABLE


def test_observe_seam_planted_table_rename_control_turns_the_pin_red(
    tmp_path: Path,
) -> None:
    """Failing control: the renamed available key is refused while 02 stays admitted."""
    rows = {k: json.loads(v) for k, v in in_process(tmp_path).items()}
    assert rows["03:planted-rename"] == {"rejected": "table"}
    assert rows["03:planted-rename"] != EXPECTED_TABLE


def test_observe_seam_in_memory_bus_projection_matches_the_in_process_one(
    tmp_path: Path,
) -> None:
    [run] = seam_run(tmp_path, profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process(tmp_path)


def test_observe_seam_broker_leg_connects_through_the_declared_lane_never_an_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing at the base: the broker leg's bus asks the lane opener for its connection.

    The opener is replaced by an in-memory bus and records what it was asked for: the declared
    lane and workspace root, and no broker address (the plaintext bootstrap leg the base used is
    refused by the SASL dev broker).
    """
    asked: list[dict[str, Any]] = []

    class LaneLikeBus:
        """The in-memory bus behind the broker client's subscribe signature (offset reset, a handle with stop)."""

        def __init__(self) -> None:
            self.bus = EventBusInmemory(
                environment="lane-start-observe-seam-lane", group="lane"
            )

        async def subscribe(
            self, topic: str, *, on_message: Any, group_id: str, auto_offset_reset: str
        ) -> Any:
            assert auto_offset_reset == "earliest"
            unsubscribe = await self.bus.subscribe(
                topic, on_message=on_message, group_id=group_id
            )

            class Handle:
                async def stop(self) -> None:
                    await unsubscribe()

            return Handle()

        async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
            await self.bus.publish(topic, key, value)

    @contextlib.asynccontextmanager
    async def fake_opener(**kwargs: Any) -> Any:
        asked.append(kwargs)
        lane_like = LaneLikeBus()
        await lane_like.bus.start()
        try:
            yield lane_like
        finally:
            await lane_like.bus.close()

    monkeypatch.setattr(
        "omnimarket.handlers.overlay_store.bus_opener.open_lane_bus", fake_opener
    )
    monkeypatch.setenv(LANE_ENV, "dev")
    monkeypatch.setenv(ROOT_ENV, str(tmp_path))
    [run] = seam_run(tmp_path, profiles=("redpanda",))
    assert asked == [
        {"bus": "kafka", "lane": "dev", "kafka_bootstrap": None, "root": tmp_path}
    ]
    assert {row.key: row.content for row in run.rows} == in_process(tmp_path)


@pytest.mark.kafka
@pytest.mark.integration
def test_observe_seam_redpanda_bus_projection_matches_the_in_memory_one(
    tmp_path: Path,
) -> None:
    if not os.environ.get(LANE_ENV) or not os.environ.get(ROOT_ENV):
        pytest.skip(
            f"Kafka not reachable: {LANE_ENV} and {ROOT_ENV} name no declared bus lane"
        )
    runs = seam_run(tmp_path, profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process(tmp_path)
    assert len(runs[1].rows) == 4
