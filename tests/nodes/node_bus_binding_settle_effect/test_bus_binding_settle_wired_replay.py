# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three recorded bus-binding-outage rows through the running bus binding (OMN-21959).

Each recorded row (tests/nodes/node_bus_binding_reducer/fixtures/bus_binding_recorded_outage_rows.json) is put on
the exception queue of a state directory whose recovery record holds no open episode, as on a host that queued the
row in an earlier episode. Then a command opens the bus through ``open_bound_bus``, the path the work-ledger append
and the SCM verbs take. With the durable bus healthy and nothing queued the row is resolved in code by the
exception actor (node_bus_binding_settle_effect); with the bus still down, or appends still queued, it stays one
open row.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omnimarket.handlers.bus_binding import outbox, select
from omnimarket.handlers.bus_binding.exception_actor import ActorPorts
from omnimarket.handlers.bus_binding.recovery import OUTAGE_CLASS, SOURCE
from omnimarket.models.bus_binding import EnumBusBinding
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
from omnimarket.nodes.node_bus_binding_settle_effect import HandlerBusBindingSettle
from omnimarket.nodes.node_bus_binding_settle_effect.handlers.handler_bus_binding_settle import (
    settle_policy,
)
from omnimarket.nodes.node_bus_binding_settle_effect.models import (
    ModelBusBindingSettleRequest,
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
from tests.unit.bus_binding.test_bus_binding_fallback import LANE, _Switch
from tests.unit.bus_binding.test_bus_binding_fallback import (
    workspace as workspace,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "node_bus_binding_reducer"
    / "fixtures"
    / "bus_binding_recorded_outage_rows.json"
)
ROWS: list[dict[str, str]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["blocks"]
NOW = datetime(2026, 11, 2, 16, tzinfo=UTC)
IDS = [row["id"][:20] for row in ROWS]


def _open_rows(state: Path) -> list[dict[str, Any]]:
    return (
        create_handler_exception_queue()
        .handle(ModelExceptionQueueRequest(state / "exception-queue", "orchestrator"))
        .rows
    )


def _put(state: Path, row: dict[str, str], *, key: str | None = None) -> str:
    result = create_handler_exception_emit().handle(
        ModelExceptionEmitRequest(
            state_dir=state / "exception-queue",
            row=ModelExceptionRow(
                cls=row["cls"],
                key=key or row["key"],
                source=row["source"],
                evidence=row["evidence"],
                tried=row["tried"],
                proposed_fix=row["proposed_fix"],
            ),
            now="2026-11-02T15:40:00Z",
        )
    )
    assert result.status == "appended"
    return result.id


def _bind(
    switch: _Switch, workspace: Path, state: Path
) -> contextlib.AbstractAsyncContextManager[select.BoundBus]:
    return select.open_bound_bus(
        bus="kafka",
        lane=LANE,
        kafka_bootstrap=None,
        workspace_root=workspace,
        state_dir=state,
        timeout_s=2.0,
        open_bus=switch,
        now=lambda: NOW,
        announce=lambda _: None,
        pending_outbox=lambda: outbox.pending_count(state),
    )


def _queue_one_append(state: Path) -> None:
    outbox.queue_request(
        state,
        ModelWorkLedgerAppendRequest(
            request_id=UUID("00000000-0000-4000-8000-000000021959"),
            rows="2026-11-02T15:40:00Z | STATUS | lane=dev | queued while degraded",
            requested_by_lane="dev",
            requesting_host="h-test",
            requested_at=NOW,
        ),
    )


def test_bus_binding_settle_contract_names_the_outage_class() -> None:
    assert settle_policy() == (SOURCE, frozenset({OUTAGE_CLASS}))
    assert {row["key"] for row in ROWS} == {f"lane={LANE}"}


@pytest.mark.parametrize("recorded", ROWS, ids=IDS)
async def test_bus_binding_settle_wired_resolves_recorded_outage_once_the_bus_is_healthy(
    workspace: Path, tmp_path: Path, recorded: dict[str, str]
) -> None:
    state = tmp_path / "state"
    case_id = _put(state, recorded)
    switch = _Switch()
    switch.up = True

    async with _bind(switch, workspace, state) as bound:
        assert bound.decision.binding is EnumBusBinding.DURABLE

    assert [row["id"] for row in _open_rows(state)] == []
    history = [
        json.loads(line)
        for line in (state / "exception-queue" / "exception-queue.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    resolves = [line for line in history if line.get("resolves") == case_id]
    assert len(resolves) == 1
    assert resolves[0]["by"] == SOURCE
    assert resolves[0]["how"] == "durable health read passed; outbox replayed, 0 queued"


@pytest.mark.parametrize("recorded", ROWS, ids=IDS)
async def test_bus_binding_settle_wired_keeps_one_outage_row_while_the_bus_is_down(
    workspace: Path, tmp_path: Path, recorded: dict[str, str]
) -> None:
    state = tmp_path / "state"
    case_id = _put(state, recorded)
    switch = _Switch()

    for _ in range(2):
        async with _bind(switch, workspace, state) as bound:
            assert bound.decision.binding is EnumBusBinding.INMEMORY

    outage = [row for row in _open_rows(state) if row["cls"] == OUTAGE_CLASS]
    assert [row["id"] for row in outage] == [case_id]
    assert outage[0]["evidence"] == recorded["evidence"]


@pytest.mark.parametrize("recorded", ROWS, ids=IDS)
async def test_bus_binding_settle_wired_keeps_the_row_while_appends_are_queued(
    workspace: Path, tmp_path: Path, recorded: dict[str, str]
) -> None:
    state = tmp_path / "state"
    case_id = _put(state, recorded)
    _queue_one_append(state)
    switch = _Switch()
    switch.up = True

    async with _bind(switch, workspace, state):
        pass

    assert [row["id"] for row in _open_rows(state)] == [case_id]


def test_bus_binding_settle_leaves_other_lanes_and_classes_alone(
    tmp_path: Path,
) -> None:
    recorded = ROWS[0]
    other_lane = _put(tmp_path, recorded, key="lane=staging")
    degraded = _put(
        tmp_path,
        {
            **recorded,
            "cls": "bus-binding-degraded",
            "evidence": "durable bus health read failed: TimeoutError: ",
        },
    )
    handler = HandlerBusBindingSettle(
        ActorPorts(durable_healthy=lambda: True, replay_outbox=lambda: 0)
    )

    result = handler.handle(
        ModelBusBindingSettleRequest(state_dir=tmp_path, lane=LANE, observed_at=NOW)
    )

    assert result.resolutions == ()
    assert {row["id"] for row in _open_rows(tmp_path)} == {other_lane, degraded}
