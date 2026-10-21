# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A customer's work ledger on the in-memory event bus (OMN-17427).

RULING 2026-10-19T21:47:42Z: a customer with no rolling markdown ledger and no
lab database still has a ledger, because the in-memory event bus stands in for
the lab bus. Every fixture row is written through ``HandlerWorkLedgerEmit`` onto
an ``EventBusInmemory``, folded back by ``InMemoryWorkLedgerQueryReader``, and
``HandlerWorkLedgerQuery`` must answer every recorded markdown read the way the
lab database reader does (``test_golden_chain_work_ledger_query``).
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.work_ledger_text import split_ledger_rows
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_event_emit_effect.handlers.inmemory_bus_emitter import (
    InMemoryBusEmitter,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
    ledger_row_topics,
)
from tests.test_golden_chain_work_ledger_query import (
    EXPECTED,
    LEDGER,
    NOW,
    request_for,
)

pytestmark = pytest.mark.unit


async def _local_ledger() -> tuple[
    EventBusInmemory, InMemoryWorkLedgerQueryReader, InMemoryBusEmitter
]:
    bus = EventBusInmemory()
    await bus.start()
    reader = InMemoryWorkLedgerQueryReader()
    await reader.attach(bus)
    return bus, reader, InMemoryBusEmitter(bus)


async def _write_fixture(emitter: InMemoryBusEmitter) -> int:
    """Write every fixture row through the real emit handler; return rows accepted.

    The handler is synchronous (a CLI's shape), so it runs on a worker thread,
    where ``InMemoryBusEmitter.handle`` finds no running loop.
    """
    handler = HandlerWorkLedgerEmit(emitter=emitter)
    accepted = 0
    for raw in split_ledger_rows(LEDGER.read_text()):
        result = await asyncio.to_thread(
            handler.handle, ModelWorkLedgerEmitRequest(row=raw)
        )
        accepted += 1 if result.accepted and result.published else 0
    return accepted


async def test_every_fixture_row_round_trips_through_the_bus() -> None:
    _, reader, emitter = await _local_ledger()
    accepted = await _write_fixture(emitter)
    assert accepted == len(split_ledger_rows(LEDGER.read_text()))
    assert len(reader) == accepted


async def test_a_redelivered_row_is_kept_once() -> None:
    _, reader, emitter = await _local_ledger()
    await _write_fixture(emitter)
    before = len(reader)
    await _write_fixture(emitter)
    assert len(reader) == before


async def test_rows_are_published_on_every_registered_ledger_topic() -> None:
    bus, _, emitter = await _local_ledger()
    await _write_fixture(emitter)
    topics = set(await bus.get_topics())
    assert topics
    assert topics <= set(ledger_row_topics())


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_query_over_the_bus_matches_the_recorded_markdown_reads(
    name: str,
) -> None:
    _, reader, emitter = await _local_ledger()
    await _write_fixture(emitter)
    node = HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW)
    case = EXPECTED[name]
    request = request_for(case)
    result = node.handle(request)
    assert result.error is None
    if request.query is EnumWorkLedgerQueryKind.ROWS:
        assert [r.text for r in result.rows] == case["rows"]
        assert result.matched == case["matched"]
    elif request.query is EnumWorkLedgerQueryKind.HOLDS:
        assert [h.row.text for h in result.holds] == case["rows"]
    elif request.query is EnumWorkLedgerQueryKind.INBOX:
        (inbox,) = result.inbox
        assert [e.row.text for e in inbox.entries] == case["opened"]
    else:
        assert [c.claim.text for c in result.open_claims] == [
            c["claim"] for c in case["claims"]
        ]


async def test_empty_ledger_answers_with_no_rows_and_no_error() -> None:
    _, reader, _ = await _local_ledger()
    request = ModelWorkLedgerQueryRequest(
        correlation_id=uuid4(), query=EnumWorkLedgerQueryKind.ROWS, now=NOW
    )
    result = HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW).handle(request)
    assert result.error is None
    assert result.rows == ()
    assert result.freshness is not None
    assert result.freshness.newest_row_ts is None


async def test_a_payload_with_no_row_is_ignored() -> None:
    reader = InMemoryWorkLedgerQueryReader()
    assert reader.ingest({"row_id": "a" * 64}) is False
    assert reader.ingest({"raw_row": "", "row_id": "a" * 64}) is False
    assert len(reader) == 0


def test_the_sync_emit_handler_writes_through_the_bus_outside_a_loop() -> None:
    """The ``ProtocolEventEmitter`` call a CLI makes: no running loop, one publish."""

    async def setup() -> tuple[EventBusInmemory, InMemoryWorkLedgerQueryReader]:
        bus = EventBusInmemory()
        await bus.start()
        reader = InMemoryWorkLedgerQueryReader()
        await reader.attach(bus)
        return bus, reader

    bus, reader = asyncio.run(setup())
    handler = HandlerWorkLedgerEmit(emitter=InMemoryBusEmitter(bus))
    row = (
        "2026-10-20T10:00:00Z | CLAIM | lane=local-a | actor=user | model=none | "
        "ticket=OMN-1 | worktree=none | host=laptop | run=r1 | scope=local ledger | "
        "est ~1 lane-hours; displaces nothing; (OMN-1)"
    )
    result = handler.handle(ModelWorkLedgerEmitRequest(row=row))
    assert result.accepted, result
    assert result.published is True
    assert (
        json.loads(next(iter(asyncio.run(bus.get_event_history()))).value)["raw_row"]
        == row
    )
    assert len(reader) == 1


async def test_sync_handle_inside_a_running_loop_says_to_await_ahandle() -> None:
    bus, _, emitter = await _local_ledger()
    with pytest.raises(
        RuntimeError, match=r"InMemoryBusEmitter\.handle.*await ahandle"
    ):
        emitter.handle(ModelEmitRequest(event_type="x", payload={}))
    del bus
