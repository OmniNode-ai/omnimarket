# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the seam deadline reading (base-first piece 3, OMN-22478).

Golden: a demanded command whose node answers in time ends in the completed event and a WORKING row.
Error: the plan's named break for piece 3, a demanded command that gets no result by its deadline,
ends in the typed failure event, a NOT_WORKING row and the seam named; a bad payload, a bus that
cannot be read and an append that does not land are typed failures too, never silence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.adapters.inmemory_seam_bus import (
    InMemorySeamBus,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.handlers.handler_seam_deadline import (
    HandlerSeamDeadline,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    EnumSeamFault,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.overlay import (
    ModelSeamDeadlineOverlay,
)

pytestmark = pytest.mark.unit

_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_seam_deadline_probe_effect/overlay.example.yaml"
)
_NOW = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)


def _overlay() -> ModelSeamDeadlineOverlay:
    return ModelSeamDeadlineOverlay.model_validate(
        yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    )


def _answering_bus(overlay: ModelSeamDeadlineOverlay) -> InMemorySeamBus:
    """A fixture node that answers known queries and gives no result for any other."""
    seam = overlay.seams[0]
    known = seam.request["query"]

    def node(payload: dict[str, str | int | bool]) -> dict[str, object] | None:
        return (
            {"query": payload["query"], "rows": []}
            if payload["query"] == known
            else None
        )

    return InMemorySeamBus({seam.command_topic: node})


def _handler(bus: InMemorySeamBus, ledger: InMemoryLedger) -> HandlerSeamDeadline:
    counter = iter(range(1000))
    return HandlerSeamDeadline(
        overlay=_overlay(),
        host="test-host",
        bus=bus,
        appender=ledger,
        clock=lambda: _NOW,
        new_id=lambda: f"00000000-0000-4000-8000-{next(counter):012d}",
    )


def _run(
    handler: HandlerSeamDeadline, fault: EnumSeamFault = EnumSeamFault.NONE
) -> list[tuple[str, dict[str, object]]]:
    command = {"correlation_id": "chain-1", "write": True, "fault": fault.value}
    out = answer_command(json.dumps(command).encode(), handler, load_topics())
    return [(topic, json.loads(body)) for topic, body in out]


def test_golden_chain_a_seam_answered_in_time_reads_working() -> None:
    topics = load_topics()
    ledger = InMemoryLedger()
    events = _run(_handler(_answering_bus(_overlay()), ledger))

    assert [topic for topic, _ in events] == [
        topics["seam_deadline_run_started"],
        topics["seam_crossing_read"],
        topics["seam_deadline_run_completed"],
    ]
    assert events[-1][0] == "onex.evt.omnimarket.seam-deadline-run-completed.v1"
    assert {body["correlation_id"] for _, body in events} == {"chain-1"}
    assert events[1][1]["crossing"] == "result"
    assert events[-1][1]["verdict"] == "WORKING"
    assert events[-1][1]["ledger"] == "written"
    (row,) = ledger.lines
    assert "| STATUS | lane=seam-deadline | src=seam-deadline |" in row
    assert "drill=no" in row
    assert "verdict=WORKING" in row
    assert "seam_example-query=WORKING:result:5ms/30000ms" in row


def test_error_chain_no_result_by_the_deadline_reads_not_working() -> None:
    """The named break: the command is one the node gives no result for."""
    topics = load_topics()
    ledger = InMemoryLedger()
    events = _run(
        _handler(_answering_bus(_overlay()), ledger), EnumSeamFault.UNANSWERABLE_COMMAND
    )

    assert [topic for topic, _ in events] == [
        topics["seam_deadline_run_started"],
        topics["seam_crossing_read"],
        topics["seam_deadline_run_failed"],
    ]
    assert {body["correlation_id"] for _, body in events} == {"chain-1"}
    final = events[-1][1]
    assert final["verdict"] == "NOT_WORKING"
    assert final["failure"] == "seam-not-crossed:example-query:no-result-by-deadline"
    (row,) = ledger.lines
    assert "drill=yes" in row
    assert "verdict=NOT_WORKING" in row
    assert "reason=example-query:no-result-by-deadline" in row


def test_error_chain_a_node_slower_than_the_deadline_reads_not_working() -> None:
    bus = _answering_bus(_overlay())
    bus.latency_ms = 31_000
    ledger = InMemoryLedger()
    events = _run(_handler(bus, ledger))
    assert events[-1][0] == load_topics()["seam_deadline_run_failed"]
    assert events[1][1]["crossing"] == "no-result-by-deadline"
    assert "drill=no" in ledger.lines[0]
    assert "verdict=NOT_WORKING" in ledger.lines[0]


def test_error_chain_an_error_answer_reads_not_working() -> None:
    seam = _overlay().seams[0]
    bus = InMemorySeamBus({seam.command_topic: lambda _p: {"error": "db down"}})
    events = _run(_handler(bus, InMemoryLedger()))
    assert events[1][1]["crossing"] == "result-error"
    assert events[-1][1]["verdict"] == "NOT_WORKING"


def test_error_chain_a_typed_failure_terminal_reads_not_working() -> None:
    """The runtime answers a command it cannot handle with a failure on the result topic."""
    seam = _overlay().seams[0]
    bus = InMemorySeamBus(
        {seam.command_topic: lambda _p: {"status": "failed", "failure_code": "x"}}
    )
    events = _run(_handler(bus, InMemoryLedger()))
    assert events[1][1]["crossing"] == "result-error"
    assert events[-1][1]["verdict"] == "NOT_WORKING"


def test_error_chain_an_unreadable_bus_is_source_unreadable() -> None:
    bus = _answering_bus(_overlay())
    bus.unreadable = "topicauthorizationfailed"
    events = _run(_handler(bus, InMemoryLedger()))
    assert events[-1][0] == load_topics()["seam_deadline_run_failed"]
    assert events[-1][1]["outcome"] == "source-unreadable"
    assert events[-1][1]["verdict"] == "NOT_WORKING"


def test_error_chain_a_bad_payload_is_a_typed_failure() -> None:
    out = answer_command(
        b'{"write": "maybe"}',
        _handler(_answering_bus(_overlay()), InMemoryLedger()),
        load_topics(),
    )
    ((topic, body),) = out
    assert topic == load_topics()["seam_deadline_run_failed"]
    assert json.loads(body)["failure"].startswith("bad-payload:")


def test_error_chain_an_append_that_does_not_land_is_a_typed_failure() -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    events = _run(_handler(_answering_bus(_overlay()), ledger))
    assert events[-1][0] == load_topics()["seam_deadline_run_failed"]
    assert events[-1][1]["ledger"] == "append-failed:refused"
    assert ledger.lines == []


def test_the_fault_zero_deadline_flips_a_working_seam() -> None:
    events = _run(
        _handler(_answering_bus(_overlay()), InMemoryLedger()),
        EnumSeamFault.DEADLINE_ZERO,
    )
    assert events[-1][1]["verdict"] == "NOT_WORKING"


def test_a_seam_the_overlay_does_not_declare_is_refused() -> None:
    handler = _handler(_answering_bus(_overlay()), InMemoryLedger())
    command = {"correlation_id": "c", "seams": ["nope"], "write": True}
    out = answer_command(json.dumps(command).encode(), handler, load_topics())
    assert json.loads(out[-1][1])["failure"] == "seam-not-declared:nope"


def test_a_redelivered_run_does_not_write_twice() -> None:
    ledger = InMemoryLedger()
    handler = _handler(_answering_bus(_overlay()), ledger)
    _run(handler)
    events = _run(handler)
    assert events[-1][1]["ledger"] == "already-written"
    assert len(ledger.lines) == 1
