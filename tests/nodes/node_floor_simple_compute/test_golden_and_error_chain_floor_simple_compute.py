# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the pure computes (OMN-19998): the lab-state verdicts and the simple floor.

Every verdict below is a function of the reads and the clock handed in; nothing here reads a file or the clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorLane,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelLabSourceDef,
    ModelLabSourceRead,
    ModelLabSourceVerdict,
    ModelLabStateInput,
    ModelLabSubject,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
    check_red_rule,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_lab_state import (
    HandlerLabState,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 5, 15, 30, tzinfo=UTC)
DEF = ModelLabSourceDef(id="hosts", title="Lab hosts", kind="hosts", max_age_s=900)


def judge(read: ModelLabSourceRead | None, at: datetime = NOW) -> ModelLabSourceVerdict:
    return (
        HandlerLabState()
        .handle(
            ModelLabStateInput(now=at, sources=(DEF,), reads=(read,) if read else ())
        )
        .verdicts[0]
    )


def read(
    status: Literal["read", "failed", "unread"] = "read",
    minutes: int = 1,
    reason: str = "",
    subjects: tuple[ModelLabSubject, ...] = (),
) -> ModelLabSourceRead:
    return ModelLabSourceRead(
        id="hosts",
        status=status,
        read_at=NOW - timedelta(minutes=minutes),
        reason=reason,
        subjects=subjects,
    )


# --- lab-state verdicts: golden and error ------------------------------------------------------------------------------


def test_golden_a_fresh_passing_read_is_working_with_its_age() -> None:
    v = judge(read(subjects=(ModelLabSubject(name="h051", ok=True),)))
    assert (v.state, v.age_s) == ("WORKING", 60)


def test_error_no_read_at_all_is_unknown_and_the_source_is_still_listed() -> None:
    v = judge(None)
    assert (v.state, v.id, v.reason) == ("UNKNOWN", "hosts", "nothing read this source")


def test_error_an_unread_source_is_unknown_with_the_readers_reason() -> None:
    v = judge(ModelLabSourceRead(id="hosts", status="unread", reason="ssh host unset"))
    assert (v.state, v.reason) == ("UNKNOWN", "ssh host unset")


def test_error_a_failed_read_is_not_working() -> None:
    v = judge(read("failed", reason="exit 255"))
    assert (v.state, v.reason) == ("NOT WORKING", "exit 255")


def test_error_a_read_one_second_over_the_limit_is_stale_and_not_working() -> None:
    assert judge(read(minutes=15)).state == "WORKING"
    v = judge(
        ModelLabSourceRead(
            id="hosts", status="read", read_at=NOW - timedelta(seconds=901)
        )
    )
    assert v.state == "NOT WORKING"
    assert v.reason.startswith("stale: read 15 min ago")


def test_error_one_failing_subject_makes_the_whole_source_not_working() -> None:
    v = judge(
        read(
            subjects=(
                ModelLabSubject(name="h051", ok=True),
                ModelLabSubject(name="h101", ok=False, note="disk full"),
            )
        )
    )
    assert (v.state, v.reason) == ("NOT WORKING", "h101: disk full")


def test_error_a_read_dated_in_the_future_is_not_working() -> None:
    v = judge(read(minutes=-30))
    assert v.state == "NOT WORKING"
    assert "future" in v.reason


# --- the simple floor --------------------------------------------------------------------------------------------------


def floor_input(
    *,
    lanes: tuple[ModelFloorLane, ...] = (),
    reds: int = 3,
    go_given_through: int | None = 0,
) -> ModelFloorSimpleInput:
    overlay = ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )
    return ModelFloorSimpleInput(
        now=NOW,
        overlay=overlay,
        open_prs=10,
        open_prs_before=12,
        merges_in_window=4,
        watcher_read_at=NOW - timedelta(minutes=1),
        slots_working=5,
        slots_total=20,
        record_read_at=NOW - timedelta(minutes=1),
        pr_class_counts={"red": reds},
        pr_class_tickets={"red": ("OMN-1",)},
        pr_class_prs={"red": ("r#1",)},
        lanes=lanes,
        lab=None,
        receipts=None,
        go_given_through=go_given_through,
    )


def test_golden_one_sentence_three_numbers_and_the_rebuild_strip() -> None:
    doc = HandlerFloorSimple().handle(floor_input())
    assert len(doc.numbers) == 3
    assert [n.value for n in doc.numbers] == ["4", "10", "5 of 20"]
    assert "down 2" in doc.numbers[1].label
    assert doc.rebuild.headline == "Piece 1, code graph: not started."
    assert doc.rebuild.next_text == "Waiting on your go. Next is hosts and capacity."
    assert doc.sentence.count(".") == 1


def test_golden_a_cause_with_a_live_lane_is_amber_and_one_without_is_red() -> None:
    # A lane on one of the class's pull requests is on the cause, whatever ticket it carries.
    for ticket in ("OMN-19998", "OMN-1", ""):
        lane = ModelFloorLane(lane="fix-1", ticket=ticket, pr="r#1")
        doc = HandlerFloorSimple().handle(floor_input(lanes=(lane,)))
        cause = next(c for c in doc.causes if c.id == "prs:red")
        assert (cause.state, cause.lanes) == ("amber", ("fix-1",))
    bare = HandlerFloorSimple().handle(floor_input())
    nobody = next(c for c in bare.causes if c.id == "prs:red")
    assert (nobody.state, nobody.work_under_way) == ("red", False)


def test_error_a_lane_that_only_shares_a_ticket_with_the_cause_is_not_on_it() -> None:
    # The watcher names OMN-1 where it saw the red pull requests and the landing ticket is on every landing lane; neither
    # says the lane works on a red pull request, so the cause stays red with nobody on it.
    for ticket in ("OMN-19998", "OMN-1"):
        lane = ModelFloorLane(lane="fix-1", ticket=ticket, pr="r#2")
        doc = HandlerFloorSimple().handle(floor_input(lanes=(lane,)))
        cause = next(c for c in doc.causes if c.id == "prs:red")
        assert (cause.state, cause.lanes) == ("red", ())


def test_golden_the_biggest_cause_comes_first() -> None:
    doc = HandlerFloorSimple().handle(
        floor_input().model_copy(
            update={"pr_class_counts": {"red": 9, "queue-stuck": 20}}
        )
    )
    assert [c.id for c in doc.causes][:2] == ["prs:queue-stuck", "prs:red"]


def test_error_a_lane_that_is_stalled_is_not_work_under_way() -> None:
    stalled = ModelFloorLane(lane="fix-3", pr="r#1", stalled=True)
    cause = next(
        c
        for c in HandlerFloorSimple().handle(floor_input(lanes=(stalled,))).causes
        if c.id == "prs:red"
    )
    assert cause.state == "red"


def test_error_a_needs_operator_cause_is_red_even_with_a_lane_on_it() -> None:
    base = floor_input(lanes=(ModelFloorLane(lane="fix-2", pr="r#1"),))
    overlay = base.overlay.model_copy(
        update={
            "causes": base.overlay.causes.model_copy(
                update={"needs_operator": ("prs:red",)}
            )
        }
    )
    doc = HandlerFloorSimple().handle(base.model_copy(update={"overlay": overlay}))
    cause = next(c for c in doc.causes if c.id == "prs:red")
    assert (cause.state, cause.needs_operator) == ("red", True)
    # a red row that needs the operator is allowed: the colour rule has no violation and a live lane carries it
    assert doc.red_rule.violations == ()
    assert "prs:red" not in doc.red_rule.uncarried_red


def test_error_an_unread_input_is_unread_and_a_cause_never_zero() -> None:
    doc = HandlerFloorSimple().handle(
        floor_input().model_copy(
            update={
                "open_prs": None,
                "merges_in_window": None,
                "watcher_read_at": None,
                "sources_read": {"pull_request_watcher": False},
            }
        )
    )
    assert [n.value for n in doc.numbers[:2]] == ["unread", "unread"]
    assert "source:pull_request_watcher" in {c.id for c in doc.causes}


def test_error_a_stale_watcher_makes_the_numbers_unread() -> None:
    doc = HandlerFloorSimple().handle(
        floor_input().model_copy(update={"watcher_read_at": NOW - timedelta(hours=2)})
    )
    assert doc.numbers[0].value == "unread"


def test_error_the_colour_rule_reading_fails_on_a_planted_red_cause_with_work_under_way() -> (
    None
):
    lanes = (ModelFloorLane(lane="fix-2", pr="r#1"),)
    good = HandlerFloorSimple().handle(floor_input(lanes=lanes))
    planted = good.model_copy(
        update={
            "causes": tuple(
                c.model_copy(update={"state": "red"}) if c.id == "prs:red" else c
                for c in good.causes
            )
        }
    )
    reading = check_red_rule(planted, lanes)
    assert reading.state == "NOT WORKING"
    assert reading.red_with_work_under_way == 1
    assert reading.violations == ("prs:red draws red while fix-2 is on it",)


def test_error_a_row_drawn_red_with_no_cause_behind_it_is_caught() -> None:
    doc = HandlerFloorSimple().handle(floor_input())
    planted = doc.model_copy(
        update={
            "pieces": (
                doc.pieces[0].model_copy(update={"draw": "red"}),
                *doc.pieces[1:],
            )
        }
    )
    reading = check_red_rule(planted, ())
    assert reading.state == "NOT WORKING"
    assert "piece 1 draws red with no cause behind it" in reading.violations


def test_error_the_overlay_must_declare_each_of_the_18_pieces_once() -> None:
    raw = overlay_text("node_floor_simple_compute")
    raw["pieces"][5]["piece"] = 4
    with pytest.raises(ValueError, match="each of the pieces 1 to 18 once"):
        ModelFloorSimpleOverlay.model_validate(raw)
