# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Detector and actor for the classes bus-binding-degraded, bus-binding-outage and bus-binding-outbox-refused.

The scenario replays the recorded evidence blocks (fixtures/bus_binding_recorded_rows.json) through the detector
and the actor against an exception queue in a temporary directory: a case code can resolve is resolved and
nothing is emitted; a case it cannot resolve is emitted once, with what it tried.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.bus_binding.detector import classify_block
from omnimarket.handlers.bus_binding.exception_actor import (
    ActorPorts,
    settle_case,
)
from omnimarket.handlers.bus_binding.recovery import (
    ALARM_CLASS,
    OUTAGE_CLASS,
    REFUSED_CLASS,
)
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

FIXTURE = Path(__file__).parent / "fixtures" / "bus_binding_recorded_rows.json"
BLOCKS: list[dict[str, str]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["blocks"]
NOW = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
LEDGER_DIGEST = "a" * 64
REQUEST_DIGEST = "b" * 64


def block(cls: str, index: int = 0) -> dict[str, str]:
    return [b for b in BLOCKS if b["cls"] == cls][index]


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


def put_in_queue(state_dir: Path, row: dict[str, str]) -> str:
    result = create_handler_exception_emit().handle(
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
            now="2026-11-02T02:50:00Z",
        )
    )
    return result.id


@pytest.mark.parametrize("recorded", BLOCKS, ids=[b["cls"] for b in BLOCKS])
def test_bus_binding_exception_actor_detector_recognises_each_recorded_block(
    recorded: dict[str, str],
) -> None:
    assert classify_block(recorded) == recorded["cls"]


def test_bus_binding_exception_actor_detector_ignores_other_classes() -> None:
    other = {**block(ALARM_CLASS), "cls": "ci-red", "evidence": "check lint failed"}
    assert classify_block(other) is None


@pytest.mark.parametrize("cls", [ALARM_CLASS, OUTAGE_CLASS])
def test_bus_binding_exception_actor_resolves_recovered_binding_in_code(
    tmp_path: Path, cls: str
) -> None:
    recorded = block(cls)
    case_id = put_in_queue(tmp_path, recorded)
    assert [row["id"] for row in open_rows(tmp_path)] == [case_id]

    resolution = settle_case(
        tmp_path, {**recorded, "id": case_id}, ports(healthy=True), now=NOW
    )

    assert resolution.resolved is True
    assert resolution.emitted == ""
    assert open_rows(tmp_path) == []


@pytest.mark.parametrize("cls", [ALARM_CLASS, OUTAGE_CLASS])
def test_bus_binding_exception_actor_emits_only_a_case_it_cannot_resolve(
    tmp_path: Path, cls: str
) -> None:
    recorded = block(cls)

    still_down = settle_case(tmp_path, recorded, ports(healthy=False), now=NOW)
    assert still_down.resolved is False
    assert still_down.emitted.startswith("appended:")
    assert [row["cls"] for row in open_rows(tmp_path)] == [cls]

    # The same open case reported again stays one row.
    again = settle_case(tmp_path, recorded, ports(healthy=False), now=NOW)
    assert again.emitted.startswith("repeat:")
    assert len(open_rows(tmp_path)) == 1


def test_bus_binding_exception_actor_outbox_left_unreplayed_is_not_resolved(
    tmp_path: Path,
) -> None:
    resolution = settle_case(
        tmp_path, block(ALARM_CLASS), ports(healthy=True, remaining=3), now=NOW
    )
    assert resolution.resolved is False
    assert "3" in resolution.how
    assert len(open_rows(tmp_path)) == 1


def refused_with_digests(ledger: str, request: str) -> dict[str, str]:
    recorded = block(REFUSED_CLASS)
    return {
        **recorded,
        "evidence": recorded["evidence"].replace(
            "request id already belongs to different rows or principal",
            "request id 3f1c2a9e-5b7d-4c1e-8a2f-6d4e9b0c7a11 already belongs to different rows or principal: "
            f"ledger digest {ledger}, request digest {request}",
        ),
    }


def test_bus_binding_exception_actor_refusal_with_equal_digests_is_a_settled_duplicate(
    tmp_path: Path,
) -> None:
    recorded = refused_with_digests(LEDGER_DIGEST, LEDGER_DIGEST)
    case_id = put_in_queue(tmp_path, recorded)

    resolution = settle_case(
        tmp_path, {**recorded, "id": case_id}, ports(healthy=True), now=NOW
    )

    assert resolution.resolved is True
    assert "duplicate" in resolution.how
    assert open_rows(tmp_path) == []


def test_bus_binding_exception_actor_refusal_with_different_digests_goes_to_the_queue(
    tmp_path: Path,
) -> None:
    recorded = refused_with_digests(LEDGER_DIGEST, REQUEST_DIGEST)
    resolution = settle_case(tmp_path, recorded, ports(healthy=True), now=NOW)
    assert resolution.resolved is False
    emitted = open_rows(tmp_path)
    assert [row["cls"] for row in emitted] == [REFUSED_CLASS]
    assert LEDGER_DIGEST in emitted[0]["evidence"]
    assert REQUEST_DIGEST in emitted[0]["evidence"]


def test_bus_binding_exception_actor_refusal_without_digests_goes_to_the_queue(
    tmp_path: Path,
) -> None:
    resolution = settle_case(
        tmp_path, block(REFUSED_CLASS), ports(healthy=True), now=NOW
    )
    assert resolution.resolved is False
    assert [row["cls"] for row in open_rows(tmp_path)] == [REFUSED_CLASS]


def test_bus_binding_exception_actor_never_resolves_a_decision_row(
    tmp_path: Path,
) -> None:
    decision = {**block(ALARM_CLASS), "decision": True}
    resolution = settle_case(tmp_path, decision, ports(healthy=True), now=NOW)
    assert resolution.resolved is False
    assert "decision" in resolution.how
    assert open_rows(tmp_path) == []
