# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The continuation of stopped lanes (OMN-23416): the golden start, the error chain and the failing controls.

One stopped entry of the floor record (``floor.stopped``) names the predecessor: its ticket, its branch and head,
its last rows and the path of its own report. A tick starts one continuation lane per candidate of the compute's
``stopped`` result, holds it under ``floor:stopped:<lane>`` and hands the started node the brief through
``floor_fill.stopped_runner_command``'s ``--brief``. The tests below read the golden behaviour, then each way the
chain can break (a report that cannot be read, a brief that cannot be written, a runner that refuses, a predecessor
whose ticket another lane still holds), then the injected breaks whose red proves the golden tests bind the behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from omnimarket.models.floor_fill import (
    EnumFloorFillEntryState,
    ModelFloorFillState,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    EnumFloorFillStatus,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.core import (
    FloorFillOrchestrator,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedBriefs,
    ScriptedFacts,
    ScriptedStarter,
    floor_record,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    OVERLAY,
    T0,
    Harness,
    harness,
    overlay,
)

pytestmark = pytest.mark.unit

TICK = EnumFloorFillCommand.TICK
SEND_KEY = EnumFloorFillCommand.SEND_KEY

LANE = "prev-0100"
COMPUTE_KEY = f"stopped:{LANE}"
STATE_KEY = f"floor:stopped:{LANE}"
REPORT_PATH = ".claude_scratch/reports/prev-0100/result.md"
REPORT_TEXT = "PREVIOUS-RESULT-TEXT: parser done, tests left"
OTHER_LANE = "prev-0200"
OTHER_REPORT_PATH = ".claude_scratch/reports/prev-0200/result.md"
OTHER_REPORT_TEXT = "PREVIOUS-RESULT-TEXT: tests still failing"


def _stopped_entry(**changes: Any) -> dict[str, Any]:
    """One stopped lane of the record: it ended partial with its slice pushed and its tests left."""
    entry: dict[str, Any] = {
        "lane": LANE,
        "ticket": "OMN-23001",
        "tickets": ["OMN-23001"],
        "outcome": "partial",
        "ended_at": "2026-11-07T11:30:00Z",
        "names": ["branch"],
        "branch": "omn-23001-slice",
        "head": "abc123def456",
        "prs": [],
        "rows": [
            "2026-11-07T11:30:00Z TERMINAL outcome=partial: pushed slice, tests left"
        ],
        "report_path": REPORT_PATH,
    }
    return {**entry, **changes}


def _second_stopped_entry() -> dict[str, Any]:
    """A second stopped lane: another ticket, another branch, its own report path."""
    return _stopped_entry(
        lane=OTHER_LANE,
        ticket="OMN-23002",
        tickets=["OMN-23002"],
        branch="omn-23002-slice",
        head="def987abc210",
        rows=["2026-11-07T11:45:00Z TERMINAL outcome=blocked: spec left"],
        report_path=OTHER_REPORT_PATH,
    )


def _harness(
    stopped: Sequence[Mapping[str, Any]],
    *,
    lanes: Sequence[Mapping[str, Any]] = (),
    lines: Mapping[str, Sequence[str]] | None = None,
    config: dict[str, Any] | None = None,
    briefs: ScriptedBriefs | None = None,
    starter: ScriptedStarter | None = None,
) -> Harness:
    """The stopped lanes in the record and no floor lines, unless the test says otherwise."""
    return harness(
        lines={k: list(v) for k, v in lines.items()} if lines else {},
        facts=ScriptedFacts(
            floor_record(
                dict(lines or {}),
                generated_at=T0,
                stopped=list(stopped),
                lanes=list(lanes),
            )
        ),
        briefs=briefs
        or ScriptedBriefs(
            reports={REPORT_PATH: REPORT_TEXT, OTHER_REPORT_PATH: OTHER_REPORT_TEXT}
        ),
        config=config,
        starter=starter,
    )


def _stored_state(h: Harness) -> ModelFloorFillState:
    read = h.store.load(h.config.scope)
    assert read.payload is not None
    return ModelFloorFillState.model_validate_json(read.payload)


def _republish(h: Harness, stopped: Sequence[Mapping[str, Any]]) -> None:
    """The lab republishes the record before the next tick: the same stopped lanes, freshly generated."""
    h.facts.floor = floor_record({}, generated_at=h.clock.now(), stopped=list(stopped))


# --------------------------------------------------------------------------- the golden behaviour


def test_a_stopped_candidate_starts_one_continuation_lane_with_the_report_in_its_brief() -> (
    None
):
    h = _harness([_stopped_entry()])
    answer = h.orchestrator().run(TICK)
    (start,) = h.starter.starts
    assert start.key == STATE_KEY
    assert start.ticket == "OMN-23001"
    # The start node reads the brief through --brief: the path the briefs seam returned.
    brief_path = f"scripted://briefs/{start.lane}.md"
    assert start.argv[start.argv.index("--brief") + 1] == brief_path
    brief = h.briefs.saved[start.lane]
    assert REPORT_TEXT in brief
    assert f"Continue lane {LANE} on ticket OMN-23001" in brief
    assert OVERLAY["stopped_continue_rule"] in brief
    assert answer.status is EnumFloorFillStatus.OK
    assert answer.exit_code == 0
    entry = _stored_state(h).entries[STATE_KEY]
    assert entry.state is EnumFloorFillEntryState.RESERVED
    assert entry.lane == start.lane


def test_the_next_tick_starts_no_second_continuation_for_the_same_stopped_lane() -> (
    None
):
    stopped = [_stopped_entry()]
    h = _harness(stopped)
    h.orchestrator().run(TICK)
    (start,) = h.starter.starts
    h.ledger.claim(start.lane, STATE_KEY)
    h.clock.advance(seconds=300)
    _republish(h, stopped)
    h.orchestrator().run(TICK)
    assert (
        len(h.starter.starts) == 1
    )  # the lane's CLAIM made the entry SENT: the key is held
    h.ledger.terminal(start.lane)
    h.clock.advance(minutes=10)  # ended, but the entry is still inside the sent window
    _republish(h, stopped)
    h.orchestrator().run(TICK)
    assert len(h.starter.starts) == 1
    h.clock.advance(minutes=121)  # past the sent window: the lane is offered again
    _republish(h, stopped)
    h.orchestrator().run(TICK)
    assert [(s.key, s.attempt) for s in h.starter.starts] == [
        (STATE_KEY, 1),
        (STATE_KEY, 2),
    ]


def test_stopped_max_lanes_bounds_the_continuations() -> None:
    h = _harness(
        [_stopped_entry(), _second_stopped_entry()],
        config=overlay(stopped_max_lanes=1),
    )
    h.orchestrator().run(TICK)
    (start,) = h.starter.starts
    assert (
        start.key == STATE_KEY
    )  # the record's first stopped lane; prev-0200 waits for room


def test_floor_lines_still_start_beside_a_stopped_candidate() -> None:
    h = _harness([_stopped_entry()], lines={"alpha": ["a1"]})
    h.orchestrator().run(TICK)
    assert {s.key for s in h.starter.starts} == {"floor:alpha:a1", STATE_KEY}


# --------------------------------------------------------------------------- a predecessor that is not there


def test_a_missing_report_still_starts_and_the_brief_says_not_supplied() -> None:
    h = _harness([_stopped_entry()], briefs=ScriptedBriefs())
    h.orchestrator().run(TICK)
    (start,) = h.starter.starts
    brief = h.briefs.saved[start.lane]
    assert "not supplied" in brief
    assert (
        REPORT_PATH in brief
    )  # the brief names where the predecessor's report would be


# --------------------------------------------------------------------------- the error chain


def test_an_unreadable_report_starts_nothing_and_names_the_lane() -> None:
    h = _harness(
        [_stopped_entry()],
        briefs=ScriptedBriefs(unreadable_reports=frozenset({REPORT_PATH})),
    )
    answer = h.orchestrator().run(TICK)
    assert h.starter.starts == []
    (refused,) = answer.key_results
    assert refused.kind == "refused"
    assert refused.key == COMPUTE_KEY
    assert (refused.cause or "").startswith("report-unreadable")
    assert f"MISSING stopped-continue:{LANE} | cause={refused.cause}" in answer.lines


def test_an_unwritable_brief_clears_the_reservation_and_reports_failed() -> None:
    h = _harness([_stopped_entry()], briefs=ScriptedBriefs(fail_save=True))
    answer = h.orchestrator().run(TICK)
    assert h.starter.starts == []
    (failed,) = answer.key_results
    assert failed.kind == "failed"
    assert failed.key == STATE_KEY
    assert failed.cause == "brief-unwritable:brief-unwritable"
    assert _stored_state(h).entries[STATE_KEY].state is EnumFloorFillEntryState.NONE


def test_a_failing_runner_clears_the_entry_and_the_next_tick_offers_it_again() -> None:
    h = _harness([_stopped_entry()], starter=ScriptedStarter(fail_all=True))
    first = h.orchestrator().run(TICK)
    (failed,) = first.key_results
    assert failed.kind == "failed"
    assert failed.cause == "exit-3"
    assert _stored_state(h).entries[STATE_KEY].state is EnumFloorFillEntryState.NONE
    h.starter.fail_all = False
    h.orchestrator().run(TICK)
    assert [(s.key, s.attempt) for s in h.starter.starts] == [
        (STATE_KEY, 1),
        (STATE_KEY, 2),
    ]


def test_a_stopped_lane_with_a_live_ticket_starts_nothing() -> None:
    h = _harness(
        [_stopped_entry()], lanes=[{"lane": "other-0100", "ticket": "OMN-23001"}]
    )
    answer = h.orchestrator().run(TICK)
    assert h.starter.starts == []
    assert answer.key_results == ()


def test_a_stopped_lane_with_no_ticket_starts_nothing() -> None:
    h = _harness([_stopped_entry(ticket="", tickets=[])])
    h.orchestrator().run(TICK)
    assert h.starter.starts == []


# --------------------------------------------------------------------------- send_key


def test_send_key_starts_a_named_stopped_candidate() -> None:
    h = _harness([_stopped_entry()])
    answer = h.orchestrator().run(SEND_KEY, COMPUTE_KEY)
    (start,) = h.starter.starts
    assert start.key == STATE_KEY
    assert f"FLOOR-SENT key={STATE_KEY} lane={start.lane}" in answer.lines
    assert answer.status is EnumFloorFillStatus.OK
    refused = h.orchestrator().run(SEND_KEY, "stopped:unknown-lane")
    assert len(h.starter.starts) == 1
    assert refused.status is EnumFloorFillStatus.FAILED
    assert [r.kind for r in refused.key_results] == ["refused"]


# --------------------------------------------------------------------------- the failing controls


def test_control_without_the_stopped_send_nothing_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WithoutTheStoppedSend: the pass never walks the stopped candidates, so the golden input starts nothing."""
    h = _harness([_stopped_entry()])
    monkeypatch.setattr(FloorFillOrchestrator, "_send_stopped", lambda *_args: False)
    h.orchestrator().run(TICK)
    assert h.starter.starts == []  # the reading the golden test asserts is one start


def test_control_without_the_reports_the_brief_lacks_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WithoutTheStoppedReports: no predecessor report is read, so the brief carries none of its result."""
    h = _harness([_stopped_entry()])
    monkeypatch.setattr(FloorFillOrchestrator, "_stopped_reports", lambda *_args: ())
    h.orchestrator().run(TICK)
    (start,) = h.starter.starts
    assert "PREVIOUS-RESULT-TEXT" not in h.briefs.saved[start.lane]
