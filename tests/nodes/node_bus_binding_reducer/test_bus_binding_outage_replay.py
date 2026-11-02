# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay of the three recorded bus-binding-outage rows through the detector and the actor (OMN-21959).

Each row is a real exception-queue block (fixtures/bus_binding_recorded_outage_rows.json). The replay puts the
row on a queue in a temporary directory, then settles it twice: with the durable bus healthy again the case is
resolved in code and the queue is empty; with the bus still down it stays one open row that says what was tried.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.bus_binding.detector import classify_block
from omnimarket.handlers.bus_binding.exception_actor import ActorPorts, settle_case
from omnimarket.handlers.bus_binding.recovery import OUTAGE_CLASS
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

FIXTURE = Path(__file__).parent / "fixtures" / "bus_binding_recorded_outage_rows.json"
ROWS: list[dict[str, str]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["blocks"]
NOW = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)


def ports(*, healthy: bool) -> ActorPorts:
    return ActorPorts(durable_healthy=lambda: healthy, replay_outbox=lambda: 0)


def open_rows(state_dir: Path) -> list[dict[str, Any]]:
    return (
        create_handler_exception_queue()
        .handle(
            ModelExceptionQueueRequest(state_dir / "exception-queue", "orchestrator")
        )
        .rows
    )


def put_in_queue(state_dir: Path, row: dict[str, str]) -> str:
    return (
        create_handler_exception_emit()
        .handle(
            ModelExceptionEmitRequest(
                state_dir=state_dir / "exception-queue",
                row=ModelExceptionRow(
                    cls=row["cls"],
                    key=row["key"],
                    source=row["source"],
                    evidence=row["evidence"],
                    tried=row["tried"],
                    proposed_fix=row["proposed_fix"],
                ),
                now="2026-11-02T03:45:00Z",
            )
        )
        .id
    )


def test_bus_binding_exception_actor_replay_holds_the_three_recorded_rows() -> None:
    assert len(ROWS) == 3
    assert {row["id"] for row in ROWS} == {
        "2026-10-27T16:35:12Z-bus-binding-outage-c45ae905",
        "2026-10-27T21:59:02Z-bus-binding-outage-c45ae905",
        "2026-10-28T06:01:23Z-bus-binding-outage-c45ae905",
    }


@pytest.mark.parametrize("recorded", ROWS, ids=[row["id"][:20] for row in ROWS])
def test_bus_binding_exception_actor_replay_detects_each_recorded_outage_row(
    recorded: dict[str, str],
) -> None:
    assert classify_block(recorded) == OUTAGE_CLASS


@pytest.mark.parametrize("recorded", ROWS, ids=[row["id"][:20] for row in ROWS])
def test_bus_binding_exception_actor_replay_resolves_each_row_once_the_bus_is_healthy(
    tmp_path: Path, recorded: dict[str, str]
) -> None:
    case_id = put_in_queue(tmp_path, recorded)
    assert [row["id"] for row in open_rows(tmp_path)] == [case_id]

    resolution = settle_case(
        tmp_path, {**recorded, "id": case_id}, ports(healthy=True), now=NOW
    )

    assert resolution.resolved is True
    assert resolution.emitted == ""
    assert open_rows(tmp_path) == []


@pytest.mark.parametrize("recorded", ROWS, ids=[row["id"][:20] for row in ROWS])
def test_bus_binding_exception_actor_replay_emits_each_row_once_while_the_bus_is_down(
    tmp_path: Path, recorded: dict[str, str]
) -> None:
    first = settle_case(tmp_path, recorded, ports(healthy=False), now=NOW)
    again = settle_case(tmp_path, recorded, ports(healthy=False), now=NOW)

    assert first.resolved is False
    assert first.emitted.startswith("appended:")
    assert again.emitted.startswith("repeat:")
    rows = open_rows(tmp_path)
    assert [row["cls"] for row in rows] == [OUTAGE_CLASS]
    assert rows[0]["evidence"] == recorded["evidence"]


def test_bus_binding_exception_actor_replay_names_the_missing_identity_it_cannot_issue(
    tmp_path: Path,
) -> None:
    no_identity, *others = ROWS
    first = settle_case(tmp_path, no_identity, ports(healthy=False), now=NOW)
    assert "lane identity" in first.how
    for other in others:
        assert (
            "lane identity"
            not in settle_case(tmp_path, other, ports(healthy=False), now=NOW).how
        )


def test_bus_binding_exception_actor_replay_never_resolves_a_decision_row(
    tmp_path: Path,
) -> None:
    for recorded in ROWS:
        resolution = settle_case(
            tmp_path, {**recorded, "decision": True}, ports(healthy=True), now=NOW
        )
        assert resolution.resolved is False
        assert open_rows(tmp_path) == []
