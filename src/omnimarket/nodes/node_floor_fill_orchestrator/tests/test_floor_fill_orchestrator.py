# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error scenarios of the fill orchestrator (plan T13 acceptance criteria 1, 2, 6, 7)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    EnumFloorFillStatus,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedFacts,
    ScriptedStarter,
    ScriptedTickets,
    floor_record,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    LINES4,
    T0,
    harness,
    overlay,
)

pytestmark = pytest.mark.unit

TICK = EnumFloorFillCommand.TICK


def test_four_lines_start_four_lanes_one_per_line() -> None:
    h = harness(config=overlay(max_fill_lanes_per_line=1))
    answer = h.orchestrator().run(TICK)
    starts = h.starter.starts
    assert len(starts) >= 4
    assert len({s.key for s in starts}) == len(starts)
    assert len({s.ticket for s in starts}) == len(starts)
    assert {s.key.split(":")[1] for s in starts} == set(LINES4)
    assert all(s.key.startswith("floor:") for s in starts)
    assert answer.status is EnumFloorFillStatus.OK
    assert answer.exit_code == 0
    # The reserved place is the line's last (max_fill_lanes_per_line = 1): T1 reads it as AT-CAPACITY.
    assert [
        line for line in answer.lines if line.startswith("NOTE line-at-capacity:")
    ] == [f"NOTE line-at-capacity:{line_id}" for line_id in ("alpha", "bravo", "delta")]
    # charlie has one task, now held by its RESERVED entry: nothing left to start on it.
    assert "NOTE line-blocked:charlie | c1=held:RESERVED" in answer.lines
    assert not any(line.startswith("MISSING") for line in answer.lines)


def test_second_pass_starts_nothing() -> None:
    h = harness()
    first = h.orchestrator().run(TICK)
    started = len(h.starter.starts)
    assert (
        started == 7
    )  # every task of the four lines: two per line at most, charlie has one
    assert first.status is EnumFloorFillStatus.OK
    # The runner's leaders append their CLAIMs; the second pass is inside the sent window.
    for s in h.starter.starts:
        h.ledger.claim(s.lane, s.key)
    h.clock.advance(minutes=5)
    second = h.orchestrator().run(TICK)
    assert len(h.starter.starts) == started
    assert second.key_results == ()


def test_send_key_refuses_held_key() -> None:
    h = harness()
    h.orchestrator().run(EnumFloorFillCommand.SEND_KEY, "floor:alpha:a1")
    assert len(h.starter.starts) == 1
    again = h.orchestrator().run(EnumFloorFillCommand.SEND_KEY, "floor:alpha:a1")
    assert len(h.starter.starts) == 1
    assert again.exit_code != 0
    assert again.status is EnumFloorFillStatus.FAILED
    assert any("held:RESERVED" in line for line in again.lines)


def test_send_key_starts_one_key_and_prints_sent() -> None:
    h = harness()
    answer = h.orchestrator().run(EnumFloorFillCommand.SEND_KEY, "floor:bravo:b2")
    (start,) = h.starter.starts
    assert start.key == "floor:bravo:b2"
    assert f"FLOOR-SENT key=floor:bravo:b2 lane={start.lane}" in answer.lines
    assert answer.exit_code == 0


def test_failed_start_clears_at_once() -> None:
    h = harness(
        config=overlay(max_fill_lanes_per_line=1),
        starter=ScriptedStarter(fail_all=True),
    )
    answer = h.orchestrator().run(TICK)
    missing = [
        line for line in answer.lines if line.startswith("MISSING line-stopped:")
    ]
    assert len(missing) == 4
    assert all("cause=start-failed:exit-3" in line for line in missing)
    # Cleared: the entries hold their ticket and no dispatch state, so the next tick offers the key again.
    h.starter.fail_all = False
    h.orchestrator().run(TICK)
    # A failed start ends its line's walk: four failed attempts, then the next tick offers each line's first key again.
    assert [s.attempt for s in h.starter.starts[:4]] == [1, 1, 1, 1]
    assert [(s.key, s.attempt) for s in h.starter.starts[4:8]] == [
        (s.key, 2) for s in h.starter.starts[:4]
    ]


def test_started_without_claim_clears_at_timeout() -> None:
    h = harness(
        config=overlay(max_fill_lanes_per_line=1), lines={"alpha": ["a1", "a2"]}
    )
    h.orchestrator().run(TICK)
    assert len(h.starter.starts) == 1
    h.clock.advance(seconds=300)
    answer = h.orchestrator().run(TICK)
    assert len(h.starter.starts) == 1
    assert any(line.startswith("NOTE line-at-capacity:alpha") for line in answer.lines)
    assert not any(line.startswith("MISSING line-stopped:") for line in answer.lines)
    h.clock.advance(seconds=301)
    h.orchestrator().run(TICK)
    attempts = [(s.key, s.attempt) for s in h.starter.starts]
    assert attempts == [("floor:alpha:a1", 1), ("floor:alpha:a1", 2)]


def test_mint_refused_next_task_starts() -> None:
    h = harness(
        config=overlay(max_fill_lanes_per_line=1),
        lines={"alpha": ["a1", "a2"]},
        tickets=ScriptedTickets(mint_refused={"floor:alpha:a1": "ticket-refused:gate"}),
    )
    answer = h.orchestrator().run(TICK)
    assert [s.key for s in h.starter.starts] == ["floor:alpha:a2"]
    assert not any(line.startswith("MISSING line-stopped:") for line in answer.lines)


def test_landing_only_starts_nothing() -> None:
    h = harness(
        facts=ScriptedFacts(floor_record(LINES4, generated_at=T0), landing_only=True)
    )
    answer = h.orchestrator().run(TICK)
    assert h.starter.starts == []
    assert answer.key_results == ()
    assert sum(1 for line in answer.lines if line.startswith("NOTE line-held:")) == 4


def test_stop_signal_starts_nothing() -> None:
    h = harness(
        facts=ScriptedFacts(
            floor_record(LINES4, generated_at=T0), stop_cause="operator"
        )
    )
    answer = h.orchestrator().run(TICK)
    assert h.starter.starts == []
    assert sum(1 for line in answer.lines if "stop-signal:operator" in line) == 4


def test_brake_and_stop_decide_before_mint() -> None:
    h = harness(
        facts=ScriptedFacts(
            floor_record(LINES4, generated_at=T0),
            landing_only=True,
            stop_cause="operator",
        )
    )
    h.orchestrator().run(TICK)
    assert not any(
        call.startswith("mint:") or call == "ticket-read" for call in h.tickets.calls
    )
    assert h.starter.starts == []


def test_existing_ticket_empty_state() -> None:
    h = harness(
        lines={"alpha": ["a1"]},
        tickets=ScriptedTickets(existing={"floor:alpha:a1": "T-PRE"}),
    )
    h.orchestrator().run(TICK)
    (start,) = h.starter.starts
    assert start.ticket == "T-PRE"
    assert h.tickets.created == []
