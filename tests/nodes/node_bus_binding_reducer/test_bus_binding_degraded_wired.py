# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus-binding-degraded actor runs in the bus binding itself (OMN-21959).

The recorded rows of the class (fixtures/bus_binding_degraded_replay.json, 897 rows of one case, key lane=dev)
are fed through ``open_bound_bus``, the path every client command takes, not through the actor alone:

* rows of the class left open in the queue are settled by the next healthy command with an empty outbox,
  whether or not this process remembers the episode that opened them;
* an episode emits one row: a degraded command after that row was closed by anyone does not append another;
* a row marked as a decision is never settled;
* a healthy read with requests still queued settles nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.bus_binding import outbox, select
from omnimarket.handlers.bus_binding.recovery import ALARM_CLASS, SOURCE
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_emit,
    create_handler_exception_queue,
    create_handler_exception_resolve,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionEmitRequest,
    ModelExceptionQueueRequest,
    ModelExceptionResolveRequest,
    ModelExceptionRow,
)
from tests.unit.bus_binding.test_bus_binding_fallback import LANE, _request, _Switch
from tests.unit.bus_binding.test_bus_binding_fallback import (
    workspace as workspace,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "bus_binding_degraded_replay.json"
SHAPES: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["shapes"]
START = datetime(2026, 11, 2, 4, tzinfo=UTC)


def _open(switch: _Switch, workspace: Path, state: Path, at: datetime) -> Any:
    return select.open_bound_bus(
        bus="kafka",
        lane=LANE,
        kafka_bootstrap=None,
        workspace_root=workspace,
        state_dir=state,
        open_bus=switch,
        now=lambda: at,
        announce=lambda _: None,
        pending_outbox=lambda: outbox.pending_count(state),
    )


def _queue(state: Path) -> Path:
    return state / "exception-queue"


def _open_rows(state: Path) -> list[dict[str, Any]]:
    if not _queue(state).exists():
        return []
    return (
        create_handler_exception_queue()
        .handle(ModelExceptionQueueRequest(_queue(state), "orchestrator"))
        .rows
    )


def _appended(state: Path) -> list[dict[str, Any]]:
    path = _queue(state) / "exception-queue.jsonl"
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text().splitlines() if line]
    return [row for row in lines if row.get("cls") == ALARM_CLASS]


def _emit_recorded(state: Path, recorded: dict[str, Any], *, decision: bool) -> str:
    result = create_handler_exception_emit().handle(
        ModelExceptionEmitRequest(
            state_dir=_queue(state),
            row=ModelExceptionRow(
                cls=recorded["cls"],
                key=recorded["key"] if not decision else "lane=dev decision",
                source=recorded["source"],
                evidence=recorded["evidence"],
                tried=recorded["tried"],
                proposed_fix=recorded["proposed_fix"],
                decision=decision,
            ),
            now="2026-11-02T03:00:00Z",
        )
    )
    assert result.status == "appended"
    return result.id


def test_the_contract_declares_the_settlement_step() -> None:
    from omnimarket.handlers.bus_binding.recovery import settlement_policy

    policy = settlement_policy()
    assert policy.alarm_scope == "episode"
    assert policy.settle_on_healthy_read == (ALARM_CLASS,)


@pytest.mark.parametrize("recorded", SHAPES, ids=[s["name"] for s in SHAPES])
async def test_a_recorded_row_left_open_is_settled_by_the_next_healthy_command(
    workspace: Path, tmp_path: Path, recorded: dict[str, Any]
) -> None:
    state = tmp_path / "state"
    case_id = _emit_recorded(state, recorded, decision=False)
    switch = _Switch()
    switch.up = True

    async with _open(switch, workspace, state, START):
        pass

    assert _open_rows(state) == []
    history = (_queue(state) / "exception-queue.jsonl").read_text().splitlines()
    resolution = json.loads(history[-1])
    assert resolution["resolves"] == case_id
    assert resolution["by"] == SOURCE
    assert "outbox replayed, 0 queued" in resolution["how"]


async def test_a_decision_row_of_the_class_is_never_settled(
    workspace: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    _emit_recorded(state, SHAPES[0], decision=True)
    switch = _Switch()
    switch.up = True

    async with _open(switch, workspace, state, START):
        pass

    operator = (
        create_handler_exception_queue()
        .handle(ModelExceptionQueueRequest(_queue(state), "operator"))
        .rows
    )
    assert [row["decision"] for row in operator] == [True]


async def test_a_healthy_read_with_requests_still_queued_settles_nothing(
    workspace: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    case_id = _emit_recorded(state, SHAPES[0], decision=False)
    outbox.queue_request(state, _request("still queued"))
    switch = _Switch()
    switch.up = True

    async with _open(switch, workspace, state, START):
        pass

    assert [row["id"] for row in _open_rows(state)] == [case_id]


async def test_one_episode_appends_one_row_however_often_its_row_is_closed(
    workspace: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    switch = _Switch()  # the durable bus stays down for the whole episode
    for tick in range(5):
        async with _open(switch, workspace, state, START + timedelta(minutes=tick)):
            pass
        for row in _open_rows(state):
            create_handler_exception_resolve().handle(
                ModelExceptionResolveRequest(
                    _queue(state), row["id"], "orchestrator", "drained", "2026-11-02"
                )
            )

    assert len(_appended(state)) == 1


async def test_the_next_episode_appends_its_own_row(
    workspace: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    switch = _Switch()
    async with _open(switch, workspace, state, START):
        pass
    switch.up = True
    async with _open(switch, workspace, state, START + timedelta(minutes=1)):
        pass
    assert _open_rows(state) == []
    switch.up = False
    async with _open(switch, workspace, state, START + timedelta(minutes=2)):
        pass

    assert len(_appended(state)) == 2
    assert len(_open_rows(state)) == 1
