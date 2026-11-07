# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the landing row (OMN-22918): publish the window command on the
in-memory bus, assert every event it produces in order under one correlation id, and inject a defined
fault. The last block names breaks that must turn the chain assertions red."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_reading import (
    ModelLandingHead,
    ModelLandingRequirements,
    ModelLandingSnapshot,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    HandlerReadingCompute,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.landing_adapters import (
    InMemoryLandingSource,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLandingRead
from tests.nodes.node_reading_writer_effect.reading_support import RUN_AT, START
from tests.unit.nodes.node_reading_writer_effect.landing_support import (
    HEAD_LIMIT,
    canary_row,
    heads,
    landing_overlay,
    snapshot,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
CORRELATION = "landing-chain-1"
OUT_TOPICS = [
    TOPICS[role]
    for role in (
        "reading_piece_written",
        "reading_piece_failed",
        "reading_window_completed",
        "reading_window_failed",
    )
]
COMMAND = json.dumps(
    {
        "correlation_id": CORRELATION,
        "window_start": START.isoformat(),
        "pieces": [9],
        "write": True,
    }
).encode()


async def _publish(
    ledger: InMemoryLedger,
    landing: object,
    compute: HandlerReadingCompute | None = None,
) -> list[tuple[str, dict[str, object]]]:
    writer = HandlerReadingWriter(
        overlay=landing_overlay(),
        source=ledger,
        appender=ledger,
        clock=lambda: RUN_AT,
        landing=landing,  # type: ignore[arg-type]
        compute=compute,
    )
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, object]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, writer, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["reading_window_completed"],
                TOPICS["reading_window_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["reading_window_requested"],
            on_message=on_command,
            group_id="reading",
        )
        for topic in OUT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"t-{topic}"
            )
        await bus.publish(TOPICS["reading_window_requested"], key=None, value=COMMAND)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def _landing_rows(ledger: InMemoryLedger) -> list[str]:
    return [
        line for line in ledger.lines if "src=reading" in line and "piece=9 " in line
    ]


def assert_landing_golden(
    events: list[tuple[str, dict[str, object]]], ledger: InMemoryLedger, verdict: str
) -> None:
    assert [topic for topic, _ in events] == [
        "onex.evt.omnimarket.reading-piece-written.v1",
        "onex.evt.omnimarket.reading-window-completed.v1",
    ]
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["piece"] == 9
    assert events[0][1]["verdict"] == verdict
    assert events[1][1]["failed"] == 0
    (row,) = _landing_rows(ledger)
    parsed = evidence_row_from_text(row)
    assert parsed is not None
    assert parsed.cells["verdict"] == verdict


def test_golden_chain_a_landing_that_holds_writes_a_working_row() -> None:
    ledger = InMemoryLedger([canary_row()])
    landing = InMemoryLandingSource(snapshot(waiting=heads(2), merges=4))
    assert_landing_golden(asyncio.run(_publish(ledger, landing)), ledger, "WORKING")


def test_golden_chain_a_held_head_writes_a_not_working_row() -> None:
    ledger = InMemoryLedger([canary_row()])
    held = (ModelLandingHead(ref="repo#9", waiting_seconds=HEAD_LIMIT + 60),)
    landing = InMemoryLandingSource(snapshot(waiting=held, merges=4))
    assert_landing_golden(asyncio.run(_publish(ledger, landing)), ledger, "NOT_WORKING")


def test_error_chain_an_unreadable_watcher_ends_in_the_typed_piece_failure_and_an_unknown_row() -> (
    None
):
    ledger = InMemoryLedger([canary_row()])
    landing = InMemoryLandingSource(error="the watcher state file is not read")
    events = asyncio.run(_publish(ledger, landing))
    assert [topic for topic, _ in events] == [
        "onex.evt.omnimarket.reading-piece-failed.v1",
        "onex.evt.omnimarket.reading-window-completed.v1",
    ]
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["outcome"] == "source-unreadable"
    assert events[1][1]["failed"] == 1
    (row,) = _landing_rows(ledger)
    parsed = evidence_row_from_text(row)
    assert parsed is not None
    assert parsed.cells["verdict"] == "UNKNOWN"


# -- named breaks: each must turn a chain assertion red ------------------------------------------


class _BlindSource:
    """Break: a landing source that never reports a waiting head, as a watcher read that drops the queue."""

    def read(
        self, requirements: ModelLandingRequirements, now: object
    ) -> ModelLandingRead:
        return ModelLandingRead(
            snapshot=ModelLandingSnapshot(age_seconds=1, heads=(), merges_in_window=0)
        )


def test_break_a_blind_source_turns_the_held_head_golden_chain_red() -> None:
    ledger = InMemoryLedger([canary_row()])
    events = asyncio.run(_publish(ledger, _BlindSource()))
    with pytest.raises(AssertionError):
        assert_landing_golden(events, ledger, "NOT_WORKING")


def test_break_a_wrong_correlation_id_turns_the_golden_chain_red() -> None:
    ledger = InMemoryLedger([canary_row()])
    events = asyncio.run(
        _publish(ledger, InMemoryLandingSource(snapshot(waiting=heads(1), merges=1)))
    )
    broken = [
        (topic, {**body, "correlation_id": "other"}) for topic, body in events[:1]
    ] + events[1:]
    with pytest.raises(AssertionError):
        assert_landing_golden(broken, ledger, "WORKING")


def test_break_a_dropped_terminal_event_turns_the_golden_chain_red() -> None:
    ledger = InMemoryLedger([canary_row()])
    events = asyncio.run(
        _publish(ledger, InMemoryLandingSource(snapshot(waiting=heads(1), merges=1)))
    )
    with pytest.raises(AssertionError):
        assert_landing_golden(events[:-1], ledger, "WORKING")
