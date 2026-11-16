# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the pilot measurement read (OMN-23619).

Publish the input event and assert every event it produces, in order, under one correlation id,
ending in the run-completed event; inject a defined fault and assert the typed failure event and
the reading on the ledger row. The last tests show the chain assertions turn red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_pilot_measurement_effect.adapters.inmemory_result_source import (
    InMemoryResultSource,
)
from omnimarket.nodes.node_pilot_measurement_effect.handlers.handler_pilot_measurement import (
    CONTRACT,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement import (
    ModelPilotResultRead,
)
from omnimarket.nodes.node_pilot_measurement_effect.protocols import PilotResultError
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_pilot_measurement_effect.pilot_measurement_support import (
    RUN_AT,
    World,
    example_overlay_document,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "pilot_measurement_written",
        "pilot_measurement_failed",
        "pilot_measurement_run_completed",
        "pilot_measurement_run_failed",
    )
]
CORRELATION = "chain-1"
PAYLOAD = json.dumps({"correlation_id": CORRELATION, "write": True}).encode()


def _good_results() -> dict[str, object]:
    return {
        "example-pilot-refactor": ModelPilotResultRead(
            metrics={
                "example_success_rate": 0.95,
                "example_tokens_per_ticket": 30000.0,
            },
            measured_at=RUN_AT,
        ),
        "example-pilot-handoff": ModelPilotResultRead(
            metrics={"example_handoffs_per_ticket": 1.0}, measured_at=RUN_AT
        ),
    }


async def _publish(
    world: World,
    ledger: InMemoryLedger | None,
    payload: bytes,
    results: dict[str, object] | None = None,
    emissions: InMemoryEmissionSink | None = None,
    overlay_document: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, object]]]:
    handler = world.handler(
        ledger=ledger,
        appender=ledger,
        results=InMemoryResultSource(
            results if results is not None else _good_results()
        ),
        emissions=emissions,
        overlay_document=overlay_document,
    )
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, object]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["pilot_measurement_run_completed"],
                TOPICS["pilot_measurement_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["pilot_measurement_requested"],
            on_message=on_command,
            group_id="pilot-measurement",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["pilot_measurement_requested"], key=None, value=payload
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def _chain(sink: InMemoryEmissionSink) -> list[tuple[str, str | None]]:
    """The piece 16 chain the run wrote to the emission journal: (phase, failure code) in order."""
    assert {e.correlation_id for e in sink.events} == {CORRELATION}
    assert {e.piece for e in sink.events} == {16}
    return [(e.phase, e.failure_code) for e in sink.events]


def _tampered_bar_hash() -> dict[str, Any]:
    document = example_overlay_document()
    document["pilots"][0]["bar_hash"] = "0" * 64
    return document


def assert_golden_chain(
    events: list[tuple[str, dict[str, object]]], verdict: str = "WORKING"
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["pilot_measurement_written"],
        TOPICS["pilot_measurement_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == verdict
    assert events[0][1]["ledger"] == "written"
    assert events[-1][1]["failed"] == 0


def test_golden_chain_ends_in_success_and_writes_one_working_row(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    sink = InMemoryEmissionSink()
    events = asyncio.run(
        _publish(World(base=tmp_path), ledger, PAYLOAD, emissions=sink)
    )
    assert_golden_chain(events)
    assert _chain(sink) == [("start", None), ("success", None)]
    assert len(ledger.lines) == 1
    row = ledger.lines[0]
    assert "lane=pilot-measurement" in row
    assert "src=pilot-measurement" in row
    assert "drill=no" in row
    assert "verdict=WORKING" in row
    assert "pilots=2" in row
    assert "accepted=2" in row
    assert "rejected=0" in row


# The contract's terminal event, spelled out so a renamed topic turns this test red.
TERMINAL_EVENT = "onex.evt.omnimarket.pilot-measurement-run-completed.v1"


def test_golden_chain_ends_in_the_contract_terminal_event(tmp_path: Path) -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT
    events = asyncio.run(_publish(World(base=tmp_path), InMemoryLedger(), PAYLOAD))
    assert events[-1][0] == TERMINAL_EVENT


def test_error_chain_a_refused_pilot_writes_a_not_working_row_and_a_typed_failure(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    sink = InMemoryEmissionSink()
    events = asyncio.run(
        _publish(
            World(base=tmp_path),
            ledger,
            PAYLOAD,
            emissions=sink,
            overlay_document=_tampered_bar_hash(),
        )
    )
    assert_golden_chain(events, verdict="NOT_WORKING")
    assert events[0][1]["outcome"] == "refused"
    assert _chain(sink) == [("start", None), ("failure", "pilot_refused")]
    assert len(ledger.lines) == 1
    assert "verdict=NOT_WORKING" in ledger.lines[0]
    assert "pilot-refused:example-pilot-refactor:" in ledger.lines[0]


def test_error_chain_an_unreadable_result_reads_unknown_and_ends_in_a_typed_failure(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    sink = InMemoryEmissionSink()
    results = _good_results()
    results["example-pilot-refactor"] = PilotResultError("result-unreadable")
    events = asyncio.run(
        _publish(World(base=tmp_path), ledger, PAYLOAD, results=results, emissions=sink)
    )
    assert_golden_chain(events, verdict="UNKNOWN")
    assert _chain(sink) == [("start", None), ("failure", "pilot_unmeasured")]
    assert "verdict=UNKNOWN" in ledger.lines[0]


def test_error_chain_a_failed_append_is_the_failed_event_and_reading_not_written(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    sink = InMemoryEmissionSink()
    events = asyncio.run(
        _publish(World(base=tmp_path), ledger, PAYLOAD, emissions=sink)
    )
    assert [topic for topic, _ in events] == [
        TOPICS["pilot_measurement_failed"],
        TOPICS["pilot_measurement_run_completed"],
    ]
    assert events[0][1]["ledger"] == "append-failed:refused"
    assert events[-1][1]["failed"] == 1
    assert _chain(sink) == [("start", None), ("failure", "reading_not_written")]
    assert ledger.lines == []


def test_error_chain_bad_payload_is_answered_with_a_typed_failure(
    tmp_path: Path,
) -> None:
    events = asyncio.run(
        _publish(World(base=tmp_path), InMemoryLedger(), b'{"write": "maybe"}')
    )
    assert [topic for topic, _ in events] == [TOPICS["pilot_measurement_run_failed"]]
    assert "bad-payload" in str(events[0][1]["failure"])


def test_error_chain_writing_run_without_appender_is_a_typed_failure(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    events = asyncio.run(_publish(World(base=tmp_path), None, PAYLOAD, emissions=sink))
    assert [topic for topic, _ in events] == [TOPICS["pilot_measurement_run_failed"]]
    assert events[0][1]["failure"] == "no-appender-for-a-writing-run"
    assert _chain(sink) == [("start", None), ("failure", "run_failed")]


def test_named_break_a_refused_pilot_turns_the_golden_assertions_red(
    tmp_path: Path,
) -> None:
    """The golden assertions are not vacuous: with a tampered bar hash both the event and the chain assertions fail."""
    sink = InMemoryEmissionSink()
    events = asyncio.run(
        _publish(
            World(base=tmp_path),
            InMemoryLedger(),
            PAYLOAD,
            emissions=sink,
            overlay_document=_tampered_bar_hash(),
        )
    )
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
    assert _chain(sink) != [("start", None), ("success", None)]
