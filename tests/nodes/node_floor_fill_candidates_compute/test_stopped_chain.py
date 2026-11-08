# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The chain from the ledger of 2026-11-07 to a continuation candidate: floor record, then candidates compute.

The floor compute reads the ledger rows and writes ``floor.stopped`` into the record; the candidates compute reads that
record text and returns the stopped lane as a candidate whose brief carries the predecessor's last rows and report and
the rule to push and continue. Golden and error chain events, the failing control of the done lane, and injected breaks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.models.floor_17a.model_floor_17a import ModelGoalScope
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    parse_ledger_text,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
    stopped_key,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.models import (
    ModelFloorEntryTicketVerdict,
    ModelFloorFillCandidatesResult,
    ModelFloorFillStateEntry,
    ModelStoppedLaneReport,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.tests.support import (
    REPORT_CHARS,
    RULE,
    request,
)
from tests.nodes.node_factory_floor_compute.support import request as floor_request
from tests.nodes.node_factory_floor_compute.test_stopped_lanes import FIXTURE

pytestmark = pytest.mark.unit

READ_AT = datetime(2026, 11, 7, 13, 30, tzinfo=UTC)
NOW = "2026-11-07T13:35:00Z"
LANE = "pieces-rest-0930"
SCOPE = ModelGoalScope(ruling="test", in_scope_tickets=("OMN-22478", "OMN-22862"))


def floor_record(
    scope: ModelGoalScope | None = SCOPE, outcomes: tuple[str, ...] | None = None
) -> str:
    made = floor_request(
        ledger=parse_ledger_text(FIXTURE.read_text(encoding="utf-8")),
        goal_scope=scope,
    )
    cfg = made.config
    if outcomes is not None:
        cfg = cfg.model_copy(
            update={"stopped": cfg.stopped.model_copy(update={"outcomes": outcomes})}
        )
    made = made.model_copy(update={"generated_at": READ_AT, "config": cfg})
    return compute_factory_floor(made).model_dump_json()


def decide(record: str, **overrides: Any) -> ModelFloorFillCandidatesResult:
    return HandlerFloorFillCandidates().handle(
        request(now=NOW, floor_record=record, **overrides)
    )


def test_golden_the_stopped_lane_is_a_candidate_with_a_brief_that_carries_its_work() -> (
    None
):
    result = decide(floor_record())
    assert result.verdict == "READ"
    assert result.stopped_verdict == "STARTABLE"
    assert [c.lane for c in result.stopped] == [LANE]
    one = result.stopped[0]
    assert one.key == stopped_key(LANE) == "stopped:pieces-rest-0930"
    assert (one.ticket, one.branch, one.head) == (
        "OMN-22478",
        "omn-22478-drift-revision-source",
        "71b6463",
    )
    assert "Continue lane pieces-rest-0930 on ticket OMN-22478" in one.brief
    assert "ended partial at 2026-11-07T10:21:09Z, naming branch" in one.brief
    assert "TERMINAL outcome=partial: Built, pushed and installed on h053" in one.brief
    assert (
        "not supplied; read .claude_scratch/reports/pieces-rest-0930/result.md"
        in one.brief
    )
    assert one.brief.endswith(f"Rule: {RULE}")
    assert result.stopped_not_ready == ()


def test_golden_the_report_text_is_inlined_and_cut() -> None:
    report = "REPORT-LINE " * 200
    result = decide(
        floor_record(), reports=(ModelStoppedLaneReport(lane=LANE, text=report),)
    )
    brief = result.stopped[0].brief
    assert "REPORT-LINE REPORT-LINE" in brief
    assert report[:REPORT_CHARS] in brief
    assert report[: REPORT_CHARS + 20] not in brief


def test_control_the_lane_that_ended_done_is_not_a_candidate_and_the_taken_ones_are_not() -> (
    None
):
    result = decide(floor_record())
    assert {c.lane for c in result.stopped} == {LANE}
    # The record never listed the done lane, the lane a successor took, or the lanes that name nothing.
    for lane in (
        "ground-state-report",
        "floor-publish-install-1010",
        "runners-r2-0930",
        "landing-owner-0900",
    ):
        assert lane not in {c.lane for c in result.stopped}


def test_error_chain_an_unread_scope_gives_no_stopped_candidate() -> None:
    result = decide(floor_record(scope=None))
    assert result.verdict == "READ"
    assert (result.stopped_verdict, result.stopped) == ("NO-STOPPED", ())


def test_error_chain_an_old_record_is_floor_unread_and_holds_no_stopped_lane() -> None:
    result = HandlerFloorFillCandidates().handle(
        request(now="2026-11-07T15:00:00Z", floor_record=floor_record())
    )
    assert (result.verdict, result.unread_cause, result.stopped) == (
        "FLOOR-UNREAD",
        "old",
        (),
    )


def test_a_sent_entry_inside_the_window_holds_the_lane() -> None:
    entry = ModelFloorFillStateEntry(key=stopped_key(LANE), status="SENT", at=NOW)
    result = decide(floor_record(), entries=(entry,))
    assert result.stopped == ()
    assert [(n.lane, n.cause) for n in result.stopped_not_ready] == [
        (LANE, "held:SENT")
    ]


def test_a_live_lane_of_the_same_name_holds_the_lane() -> None:
    import json

    record = json.loads(floor_record())
    record["floor"]["lanes"].append({"lane": LANE, "ticket": "OMN-22478"})
    result = decide(json.dumps(record))
    assert [(n.lane, n.cause) for n in result.stopped_not_ready] == [
        (LANE, f"live-lane:{LANE}")
    ]


def test_a_refused_entry_ticket_stops_the_lane_being_a_candidate() -> None:
    verdict = ModelFloorEntryTicketVerdict(
        line_id="stopped", task_id=LANE, status="refused", refusal="done"
    )
    result = decide(floor_record(), ticket_verdicts=(verdict,))
    assert result.stopped == ()
    assert result.stopped_not_ready[0].cause == "ticket-refused:done"


def test_a_usable_entry_ticket_replaces_the_predecessors_ticket() -> None:
    verdict = ModelFloorEntryTicketVerdict(
        line_id="stopped", task_id=LANE, status="usable", ticket="OMN-99999"
    )
    result = decide(floor_record(), ticket_verdicts=(verdict,))
    assert result.stopped[0].ticket == "OMN-99999"
    assert "on ticket OMN-99999" in result.stopped[0].brief


def test_capacity_the_unended_continuation_uses_the_room() -> None:
    other = ModelFloorFillStateEntry(key="stopped:other-1", status="SENT", at=NOW)
    full = decide(floor_record(), entries=(other,), stopped_max_lanes=1)
    assert (full.stopped_verdict, full.stopped, full.stopped_capacity_left) == (
        "AT-CAPACITY",
        (),
        0,
    )
    room = decide(floor_record(), entries=(other,), stopped_max_lanes=2)
    assert [c.lane for c in room.stopped] == [LANE]


def test_a_request_without_the_stopped_overlay_keys_refuses_naming_each() -> None:
    with pytest.raises(ValueError, match=r"floor_fill\.stopped_max_lanes"):
        request(stopped_max_lanes=None)


# The failing control: the done lane enters the floor's record once the outcome rule is broken.


def test_break_the_floor_listing_done_outcomes_lists_the_done_lane_in_the_record() -> (
    None
):
    import json

    real = json.loads(floor_record())["floor"]
    broken = json.loads(
        floor_record(outcomes=("partial", "blocked", "failed", "done"))
    )["floor"]
    seen = {s["lane"] for s in (*real["stopped"], *real["stopped_skipped"])}
    assert "ground-state-report" not in seen
    broke = {s["lane"] for s in (*broken["stopped"], *broken["stopped_skipped"])}
    assert "ground-state-report" in broke
