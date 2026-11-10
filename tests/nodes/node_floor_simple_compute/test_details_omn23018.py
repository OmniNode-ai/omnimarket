# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a number, a row and a piece card on the first page opens (OMN-23018).

The operator's rule: concise by default, inspectable on demand. Every number's panel lists the things the number counts and
its lines add up to the number; every red, amber and green row and every piece opens its reading, sources, owner tickets,
running lanes, pull requests, alarm history and (for a piece) its join checks. The failing controls plant a list that does not
add up and show the document says so.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorAlarmLine,
    ModelFloorAlarmSend,
    ModelFloorBay,
    ModelFloorDetail,
    ModelFloorLane,
    ModelFloorPrItem,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelLabSourceVerdict,
    ModelLabStateOutput,
    ModelLabSubject,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from tests.nodes.node_floor_simple_compute.test_golden_and_error_chain_floor_simple_compute import (
    NOW,
    floor_input,
)

pytestmark = pytest.mark.unit


def merge(i: int, repo: str) -> ModelFloorPrItem:
    return ModelFloorPrItem(
        key=f"{repo}#{i}",
        repo=repo,
        number=i,
        title=f"fix(OMN-2{i}): thing {i}",
        at=NOW - timedelta(minutes=i),
        tickets=(f"OMN-2{i}",),
    )


def opened(i: int, cls: str = "red") -> ModelFloorPrItem:
    return ModelFloorPrItem(
        key=f"omnimarket#{i}",
        repo="omnimarket",
        number=i,
        title=f"open {i}",
        cls=cls,
        reason=f"{cls}: CI Summary",
        at=NOW - timedelta(hours=i),
    )


LANES = (
    ModelFloorLane(
        lane="fix-1",
        ticket="OMN-1",
        pr="r#1",
        host="h051",
        started_at=NOW - timedelta(minutes=30),
        liveness="alive",
    ),
    ModelFloorLane(
        lane="fix-2",
        ticket="OMN-2",
        host="h051",
        started_at=NOW - timedelta(minutes=3),
        liveness="alive",
    ),
    ModelFloorLane(lane="fix-3", ticket="OMN-3", host="h053", liveness="alive"),
    ModelFloorLane(lane="unplaced-1", ticket="OMN-4", liveness="alive"),
)
BAYS = (
    ModelFloorBay(host="h051", slots=32, running=5),
    ModelFloorBay(host="h053", slots=24, running=1),
    ModelFloorBay(host="h105", slots=3, running=0, stale=True),
)


def inspected(**over: object) -> ModelFloorSimple:
    base = floor_input(lanes=LANES)
    update: dict[str, object] = {
        "merges_in_window": 3,
        "merge_items": (
            merge(1, "omnimarket"),
            merge(2, "omnimarket"),
            merge(3, "omnibase_infra"),
        ),
        "open_prs": 2,
        "open_pr_items": (opened(1), opened(2, "pending")),
        "slots_working": 6,
        "slots_total": 56,
        "bays": BAYS,
        "lanes": LANES,
        "alarms": (
            ModelFloorAlarmLine(
                alarm_id="a1",
                source_id="hosts",
                title="Hosts: NOT WORKING",
                raised_at=NOW - timedelta(minutes=20),
                delivery="delivered",
                channel="slack_alerts",
                sends=(
                    ModelFloorAlarmSend(
                        channel="slack_alerts",
                        role="primary",
                        sent_at=NOW - timedelta(minutes=19),
                        delivery="delivered",
                    ),
                ),
            ),
        ),
    }
    update.update(over)
    return HandlerFloorSimple().handle(base.model_copy(update=update))


def numbered(doc: ModelFloorSimple) -> dict[str, ModelFloorDetail]:
    return {k: v for k, v in doc.details.items() if k.startswith("number:")}


def test_golden_each_numbers_panel_lists_the_items_behind_it_and_they_add_up() -> None:
    doc = inspected()
    panels = numbered(doc)
    assert set(panels) == {
        "number:merges",
        "number:open_prs",
        "number:slots",
        "number:red_rows",
    }
    assert [n.value for n in doc.numbers] == ["3", "2", "6 of 56"]
    for key, expected in (
        ("number:merges", 3),
        ("number:open_prs", 2),
        ("number:slots", 6),
        ("number:red_rows", doc.red_rule.red_rows),
    ):
        panel = panels[key]
        assert panel.total == expected, key
        assert panel.counted == expected, key
        assert panel.reconciles, key


def test_golden_merges_are_listed_by_repository_with_their_times_and_links() -> None:
    panel = inspected().details["number:merges"]
    lines = panel.sections[0].items
    assert [i.group for i in lines] == ["omnibase_infra", "omnimarket", "omnimarket"]
    assert all(i.at is not None for i in lines)
    assert lines[0].href == "https://github.com/OmniNode-ai/omnibase_infra/pull/3"
    assert panel.facts[0].value == "3"


def test_golden_open_pull_requests_say_why_each_waits() -> None:
    panel = inspected().details["number:open_prs"]
    lines = panel.sections[0].items
    assert [i.sub for i in lines] == ["pending: CI Summary", "red: CI Summary"]
    # grouped by the overlay's title for the class when it has one, by the class otherwise
    assert {i.group for i in lines} == {"Pull requests have red checks", "pending"}


def test_golden_busy_slots_name_the_lane_ticket_host_and_start() -> None:
    panel = inspected().details["number:slots"]
    lines = panel.sections[0].items
    by_title = {i.title: i for i in lines}
    assert by_title["fix-1"].group == "h051: 5 of 32 slots busy"
    assert by_title["fix-1"].sub == "OMN-1"
    assert by_title["fix-1"].at == NOW - timedelta(minutes=30)
    assert by_title["fix-1"].href == "https://linear.app/omninode/issue/OMN-1"
    # h051 runs 5 slots and has two lane rows: the other three are named, not dropped
    rest = by_title["Busy slots with no lane row"]
    assert (rest.group, rest.count) == ("h051: 5 of 32 slots busy", 3)
    assert by_title["fix-3"].group == "h053: 1 of 24 slots busy"
    # a stale host and an unplaced lane are named and counted as zero
    headings = {s.heading: s for s in panel.sections}
    assert [i.count for i in headings["Hosts not counted"].items] == [0]
    unplaced = next(
        s for s in panel.sections if s.heading.startswith("Lanes the record places")
    )
    assert [i.title for i in unplaced.items] == ["unplaced-1"]


def test_error_more_lane_rows_than_busy_slots_are_taken_off_so_the_lines_still_add_up() -> (
    None
):
    doc = inspected(
        bays=(ModelFloorBay(host="h051", slots=32, running=1), BAYS[1]),
        slots_working=2,
    )
    panel = doc.details["number:slots"]
    assert panel.reconciles
    assert panel.counted == 2
    over = [i for i in panel.sections[0].items if i.count < 0]
    assert [(i.title, i.count) for i in over] == [
        ("Lane rows beyond the host's busy slots", -1)
    ]


def test_golden_red_rows_list_every_red_row_and_each_opens_its_own_panel() -> None:
    doc = inspected()
    panel = doc.details["number:red_rows"]
    lines = panel.sections[0].items
    assert len(lines) == doc.red_rule.red_rows >= 1
    for line in lines:
        assert line.opens in doc.details, line.opens
    assert panel.summary.startswith("Colour rule")


def test_failing_control_a_list_missing_one_item_does_not_add_up_to_its_number() -> (
    None
):
    short = inspected(merge_items=(merge(1, "omnimarket"), merge(2, "omnimarket")))
    panel = short.details["number:merges"]
    assert (panel.total, panel.counted) == (3, 2)
    assert not panel.reconciles
    extra = inspected(open_pr_items=(opened(1), opened(2), opened(3)))
    assert not extra.details["number:open_prs"].reconciles
    lost = inspected(bays=(BAYS[0],))
    assert not lost.details["number:slots"].reconciles


def test_error_an_unread_number_opens_a_panel_that_says_why_and_lists_nothing() -> None:
    doc = inspected(watcher_read_at=None, merges_in_window=None)
    panel = doc.details["number:merges"]
    assert panel.total is None
    assert panel.summary.startswith("Not read:")
    assert panel.sections[0].items == ()
    assert "no list" in panel.sections[0].heading.lower()


def test_golden_every_cause_row_opens_reading_sources_owners_lanes_prs_and_alarms() -> (
    None
):
    doc = inspected()
    assert {f"cause:{c.id}" for c in doc.causes} <= set(doc.details)
    red = doc.details["cause:prs:red"]
    headings = [s.heading for s in red.sections]
    assert headings == [
        "Owner tickets",
        "Lanes running on it",
        "Pull requests",
        "Alarm history",
    ]
    assert red.sources
    assert red.facts
    assert red.summary


def test_golden_a_lane_on_the_cause_is_listed_with_ticket_host_and_start() -> None:
    doc = inspected()
    cause = next(c for c in doc.causes if c.id == "prs:red")
    assert cause.lanes == ("fix-1",)
    lanes = doc.details["cause:prs:red"].sections[1]
    (line,) = lanes.items
    assert line.title == "fix-1"
    assert line.sub == "OMN-1 · on h051 · alive"
    assert line.at == NOW - timedelta(minutes=30)


def test_golden_a_piece_opens_its_owners_lanes_alarms_and_join_checks() -> None:
    doc = inspected(go_given_through=3)
    panel = doc.details["piece:2"]
    headings = [s.heading for s in panel.sections]
    assert headings[:5] == [
        "Owner tickets",
        "Lanes running on it",
        "Pull requests",
        "Alarm history",
        "Join checks",
    ]
    joins = next(s for s in panel.sections if s.heading == "Join checks")
    assert joins.items
    assert all(i.tag in ("in", "out") for i in joins.items)
    assert len(doc.details) >= len(doc.pieces) + len(doc.causes)


def test_golden_a_piece_not_started_opens_only_its_declared_joins() -> None:
    doc = inspected(go_given_through=0)
    started = [p for p in doc.pieces if p.status == "not_started"]
    assert started
    for p in started:
        assert [s.heading for s in doc.details[f"piece:{p.piece}"].sections] == [
            "Join checks"
        ]


def failing_lab() -> ModelLabStateOutput:
    return ModelLabStateOutput(
        computed_at=NOW,
        verdicts=(
            ModelLabSourceVerdict(
                id="hosts",
                title="Lab hosts",
                kind="hosts",
                state="NOT WORKING",
                probed_at=NOW - timedelta(minutes=2),
                published_at=NOW - timedelta(minutes=2),
                age_s=120,
                max_age_s=900,
                reason="h200 refused work",
                summary="",
                subjects=(
                    ModelLabSubject(name="h051", ok=True, note="fine"),
                    ModelLabSubject(name="h200", ok=False, note="refusing"),
                ),
                tickets=("OMN-22878",),
            ),
        ),
    )


def test_golden_a_lab_line_opens_what_was_read_its_owners_and_its_alarm_history() -> (
    None
):
    doc = inspected(lab=failing_lab())
    panel = doc.details["lab:hosts"]
    read = panel.sections[0]
    assert [(i.title, i.tag) for i in read.items] == [
        ("h051", "ok"),
        ("h200", "failing"),
    ]
    assert read.items[1].tone == "red"
    history = next(s for s in panel.sections if s.heading == "Alarm history")
    assert [i.tag for i in history.items] == ["raised", "send"]
    owners = next(s for s in panel.sections if s.heading == "Owner tickets")
    assert owners.items[0].href == "https://linear.app/omninode/issue/OMN-22878"
    assert panel.sources
    assert "hosts" in panel.sources[0]


def test_golden_the_open_alarms_panel_carries_every_alarm_without_a_receipt() -> None:
    doc = inspected()
    alarm = doc.details["alarms:open"]
    assert alarm.sections[0].heading == "Alarm history"
    assert alarm.summary == "0 of 1 open alarms have no delivery receipt."


def test_error_the_details_are_part_of_the_served_document_and_survive_a_round_trip() -> (
    None
):
    doc = inspected()
    again = ModelFloorSimple.model_validate_json(doc.model_dump_json())
    assert again.details.keys() == doc.details.keys()
    assert again.details["number:merges"].reconciles


def test_golden_a_floor_input_without_the_new_lists_still_builds_every_panel() -> None:
    # A tick that predates the lists (or a test fixture) gives the number and no lines: the panel says it does not add up.
    base: ModelFloorSimpleInput = floor_input()
    doc = HandlerFloorSimple().handle(base)
    assert not doc.details["number:merges"].reconciles
    assert doc.details["number:red_rows"].reconciles
