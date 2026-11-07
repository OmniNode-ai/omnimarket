# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-22903): the relation map's progress is written as a reading row the floor can show.

The row is a STATUS row of the reading writer's lane with ``src=reading``; the floor's own reader
(``reading_rows``) must read it as piece 1's reading. Golden and error chain event tests run on the
in-memory bus; the named injected breaks (a dropped append, a mutated progress cell) turn them red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.floor_17a import ModelLedgerReadingRule
from omnimarket.models.import_relations import (
    ModelRelationMapReadingCommand,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import reading_rows
from omnimarket.nodes.node_network_graph_compute.handlers.handler_import_relation_compute import (
    HandlerImportRelationCompute,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    load_topics,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_relation_map_reading import (
    HandlerRelationMapReading,
    answer_relation_map_command,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingPublish
from tests.unit.nodes.node_network_graph_compute.relation_support import (
    edge,
    example_overlay,
    request,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
PUBLISH = ModelReadingPublish(
    lane="reading-writer",
    actor="node:node_reading_writer_effect",
    publish_max_age_seconds=7200,
    reading_term="src=reading",
)
NOW = datetime(2026, 11, 7, 12, 30, tzinfo=UTC)
WINDOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
CORRELATION = "relation-map-1"
FIVE = (
    ("dispatch", "ledger-session"),
    ("dispatch", "tickets-plans"),
    ("landing", "tickets-plans"),
    ("intelligence-memory", "seams-verify"),
    ("intelligence-memory", "landing"),
)


def _report(drop: int | None = None) -> Any:
    edges = tuple(
        edge(f"{s}:f.py", f"{t}:g.py") for i, (s, t) in enumerate(FIVE) if i != drop
    )
    return HandlerImportRelationCompute().handle(request(edges, example_overlay()))


def _command(report: Any, **changes: object) -> bytes:
    body: dict[str, Any] = {
        "correlation_id": CORRELATION,
        "piece": 1,
        "window_start": WINDOW.isoformat(),
        "report": None if report is None else json.loads(report.model_dump_json()),
        "write": True,
    }
    body.update(changes)
    return json.dumps(body).encode()


def _writer(ledger: InMemoryLedger) -> HandlerRelationMapReading:
    return HandlerRelationMapReading(
        publish=PUBLISH, appender=ledger, clock=lambda: NOW
    )


async def _publish(
    ledger: InMemoryLedger,
    payload: bytes,
    answer: Callable[..., list[tuple[str, bytes]]] = answer_relation_map_command,
) -> list[tuple[str, dict[str, object]]]:
    """Publish the command on the in-memory bus; return every event produced, in order."""
    writer = _writer(ledger)
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, object]]] = []
    done = asyncio.Event()
    outcomes = (TOPICS["relation_map_written"], TOPICS["relation_map_failed"])

    async def on_command(message: Any) -> None:
        for topic, body in answer(message.value, writer, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["relation_map_requested"], on_message=on_command, group_id="map"
        )
        for topic in outcomes:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(TOPICS["relation_map_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def _rule() -> ModelLedgerReadingRule:
    return ModelLedgerReadingRule(
        row_type="STATUS",
        lane=PUBLISH.lane,
        cells={"src": "reading"},
        lookback_days=3,
        piece_cell="piece",
        verdict_cell="verdict",
        probed_at_cell="probed_at",
        published_at_cell="published_at",
        max_age_cell="max_age",
        reason_cell="reason",
    )


def assert_golden(
    events: list[tuple[str, dict[str, object]]], ledger: InMemoryLedger
) -> None:
    assert [topic for topic, _ in events] == [TOPICS["relation_map_written"]]
    assert TOPICS["relation_map_written"] == (
        "onex.evt.omnimarket.relation-map-reading-written.v1"
    )
    body = events[0][1]
    assert body["correlation_id"] == CORRELATION
    assert body["verdict"] == "WORKING"
    assert body["reason"] == "mapped=5/5,findings=0,cycles=0"
    lines = [row for row in ledger.lines if "name=relation_map" in row]
    assert len(lines) == 1, lines
    line = lines[0]
    row = evidence_row_from_text(line)
    assert row is not None
    assert row.cells["reason"] == "mapped=5/5,findings=0,cycles=0"


def test_golden_chain_the_map_progress_is_one_reading_row_and_one_written_event() -> (
    None
):
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(ledger, _command(_report())))
    assert_golden(events, ledger)


def test_the_floor_reads_the_row_as_piece_one_working() -> None:
    ledger = InMemoryLedger()
    asyncio.run(_publish(ledger, _command(_report())))
    rows = [evidence_row_from_text(line) for line in ledger.lines]
    readings, rejected = reading_rows([r for r in rows if r is not None], _rule())
    assert rejected == 0
    assert [(r.piece, r.state, r.reason) for r in readings] == [
        (1, "WORKING", "mapped=5/5,findings=0,cycles=0")
    ]
    assert readings[0].max_age_s == PUBLISH.publish_max_age_seconds


def test_progress_and_findings_show_in_the_row_while_the_verdict_stays_working() -> (
    None
):
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(ledger, _command(_report(drop=2))))
    assert events[0][1]["verdict"] == "WORKING"
    assert events[0][1]["reason"] == "mapped=5/5,findings=1,cycles=0"
    assert "finding=landing-to-tickets-plans:not-in-graph" in ledger.lines[0]


def test_error_chain_an_unread_graph_is_an_unknown_row_and_a_written_event() -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(ledger, _command(None)))
    assert events[0][1]["verdict"] == "UNKNOWN"
    assert events[0][1]["reason"] == "graph-unread"
    assert "verdict=UNKNOWN" in ledger.lines[0]
    assert "outcome=unread" in ledger.lines[0]


def test_error_chain_a_refused_append_is_the_typed_failed_event() -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    events = asyncio.run(_publish(ledger, _command(_report())))
    assert [topic for topic, _ in events] == [TOPICS["relation_map_failed"]]
    assert events[0][1]["failure"] == "append-failed:refused"
    assert events[0][1]["correlation_id"] == CORRELATION
    assert ledger.lines == []


def test_error_chain_a_bad_payload_is_the_typed_failed_event_never_dropped() -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(ledger, b'{"piece": 0}'))
    assert [topic for topic, _ in events] == [TOPICS["relation_map_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload:")
    assert ledger.lines == []


def test_the_same_window_and_content_appends_once() -> None:
    ledger = InMemoryLedger()
    writer = _writer(ledger)
    command = ModelRelationMapReadingCommand.model_validate_json(_command(_report()))
    assert writer.handle(command).status == "written"
    assert writer.handle(command).status == "already-written"
    assert len(ledger.lines) == 1


def test_a_probe_computes_the_row_and_appends_nothing() -> None:
    ledger = InMemoryLedger()
    command = ModelRelationMapReadingCommand.model_validate_json(
        _command(_report(), write=False)
    )
    result = _writer(ledger).handle(command)
    assert result.status == "not-written"
    assert result.row is not None
    assert ledger.lines == []


# -- the injected breaks: each chain assertion must be able to fail -------------------------------


def test_break_a_dropped_append_turns_the_golden_assertion_red() -> None:
    class Dropping(HandlerRelationMapReading):
        def handle(self, command: Any) -> Any:
            return super().handle(command.model_copy(update={"write": False}))

    def answer(payload: bytes, handler: Any, topics: dict[str, str]) -> Any:
        return answer_relation_map_command(
            payload,
            Dropping(publish=PUBLISH, appender=handler._appender, clock=lambda: NOW),
            topics,
        )

    ledger = InMemoryLedger()
    events = asyncio.run(_publish(ledger, _command(_report()), answer))
    with pytest.raises(AssertionError):
        assert_golden(events, ledger)


def test_break_a_mutated_progress_cell_turns_the_golden_assertion_red() -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(ledger, _command(_report())))
    ledger.lines[0] = ledger.lines[0].replace("mapped=5/5", "mapped=0/5")
    with pytest.raises(AssertionError):
        assert_golden(events, ledger)
