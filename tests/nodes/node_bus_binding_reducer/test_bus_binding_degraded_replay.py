# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Friction replay scenario for the exception class bus-binding-degraded (OMN-21959).

The queue held 897 rows of one case (key lane=dev) in four evidence shapes. The scenario replays each recorded
shape (fixtures/bus_binding_degraded_replay.json) through the detector and the actor against an exception queue
in a temporary directory, with the durable broker recovered and with it still down: a recovered broker resolves
every shape in code and emits nothing; a broker still down leaves one open row, and the shape that names a
missing lane identity says so in what was tried.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.bus_binding.detector import classify_block
from omnimarket.handlers.bus_binding.exception_actor import ActorPorts, settle_case
from omnimarket.handlers.bus_binding.recovery import ALARM_CLASS
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_emit,
    create_handler_exception_queue,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionEmitRequest,
    ModelExceptionQueueRequest,
    ModelExceptionRow,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "bus_binding_degraded_replay.json"
RECORDED: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
SHAPES: list[dict[str, Any]] = RECORDED["shapes"]
SHAPE_IDS = [shape["name"] for shape in SHAPES]
NOW = datetime(2026, 11, 2, 4, 5, tzinfo=UTC)
IDENTITY_SHAPE = "no-lane-identity"


def shape(name: str) -> dict[str, Any]:
    return next(s for s in SHAPES if s["name"] == name)


def ports(*, healthy: bool, remaining: int = 0) -> ActorPorts:
    return ActorPorts(durable_healthy=lambda: healthy, replay_outbox=lambda: remaining)


def open_rows(state_dir: Path) -> list[dict[str, Any]]:
    queue = state_dir / "exception-queue"
    if not queue.exists():
        return []
    return (
        create_handler_exception_queue()
        .handle(ModelExceptionQueueRequest(queue, "orchestrator"))
        .rows
    )


def queue_the_shape(state_dir: Path, recorded: dict[str, Any]) -> str:
    result = create_handler_exception_emit().handle(
        ModelExceptionEmitRequest(
            state_dir=state_dir / "exception-queue",
            row=ModelExceptionRow(
                cls=recorded["cls"],
                key=recorded["key"],
                source=recorded["source"],
                evidence=recorded["evidence"],
                tried=recorded["tried"],
                proposed_fix=recorded["proposed_fix"],
            ),
            now="2026-11-02T04:00:00Z",
        )
    )
    assert result.status == "appended"
    return result.id


def test_bus_binding_exception_actor_degraded_replay_fixture_holds_the_recorded_rows() -> (
    None
):
    assert sum(s["count"] for s in SHAPES) == RECORDED["total_rows"] == 897
    assert len(SHAPES) == 4
    assert {s["key"] for s in SHAPES} == {"lane=dev"}


@pytest.mark.parametrize("name", SHAPE_IDS)
def test_bus_binding_exception_actor_degraded_replay_detector_recognises_the_shape(
    name: str,
) -> None:
    assert classify_block(shape(name)) == ALARM_CLASS


@pytest.mark.parametrize("name", SHAPE_IDS)
def test_bus_binding_exception_actor_degraded_replay_recovered_broker_resolves_the_shape(
    tmp_path: Path, name: str
) -> None:
    case_id = queue_the_shape(tmp_path, shape(name))
    assert [row["id"] for row in open_rows(tmp_path)] == [case_id]

    resolution = settle_case(
        tmp_path, {**shape(name), "id": case_id}, ports(healthy=True), now=NOW
    )

    assert resolution.cls == ALARM_CLASS
    assert resolution.resolved is True
    assert resolution.emitted == ""
    assert open_rows(tmp_path) == []


def test_bus_binding_exception_actor_degraded_replay_all_shapes_in_a_row_leave_the_queue_empty(
    tmp_path: Path,
) -> None:
    for recorded in SHAPES:
        case_id = queue_the_shape(tmp_path, recorded)
        resolution = settle_case(
            tmp_path, {**recorded, "id": case_id}, ports(healthy=True), now=NOW
        )
        assert resolution.resolved is True
    assert open_rows(tmp_path) == []


@pytest.mark.parametrize("name", SHAPE_IDS)
def test_bus_binding_exception_actor_degraded_replay_broker_still_down_emits_one_open_row(
    tmp_path: Path, name: str
) -> None:
    first = settle_case(tmp_path, shape(name), ports(healthy=False), now=NOW)
    assert first.resolved is False
    assert first.emitted.startswith("appended:")

    for recorded in SHAPES:
        again = settle_case(tmp_path, recorded, ports(healthy=False), now=NOW)
        assert again.resolved is False
        assert again.emitted.startswith("repeat:")

    rows = open_rows(tmp_path)
    assert [row["cls"] for row in rows] == [ALARM_CLASS]
    assert rows[0]["evidence"] == shape(name)["evidence"]


def test_bus_binding_exception_actor_degraded_replay_missing_identity_is_named_in_what_was_tried(
    tmp_path: Path,
) -> None:
    resolution = settle_case(
        tmp_path, shape(IDENTITY_SHAPE), ports(healthy=False), now=NOW
    )
    assert resolution.resolved is False
    assert "lane identity" in resolution.how
    assert "lane identity" in open_rows(tmp_path)[0]["tried"]


def test_bus_binding_exception_actor_degraded_replay_a_read_that_fails_otherwise_does_not_name_identity(
    tmp_path: Path,
) -> None:
    resolution = settle_case(
        tmp_path, shape("health-read-timeout"), ports(healthy=False), now=NOW
    )
    assert "lane identity" not in resolution.how
