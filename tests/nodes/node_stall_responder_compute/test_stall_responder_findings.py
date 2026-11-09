# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One test per Tessl Review finding on the stall responder and the idle-cost sum (OMN-23192).

Each test fails on omnimarket dev 566d7ead09 and passes with the fix.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_stall_responder_compute.handlers.handler_lab_idle_cost import (
    HandlerLabIdleCost,
)
from omnimarket.nodes.node_stall_responder_compute.handlers.handler_stall_cause_responder import (
    HandlerStallCauseResponder,
)
from omnimarket.nodes.node_stall_responder_compute.models.model_stall_responder import (
    ModelLabIdleCostRequest,
    ModelStallResponderRequest,
    ModelStallResponderResult,
)

pytestmark = pytest.mark.unit

LONG_PREFIX = "a-subject-name-that-is-longer-than-the-forty-character-slug-limit"


def _stall_row(stamp: str, findings: str) -> str:
    return (
        f"{stamp} | MSG | from=launchd-throughput-tick | principal=p | to=orchestrator-1 | "
        f"ticket=OMN-20800 | req=r | via=bus:h200 | throughput-tick is not OK: "
        f"THROUGHPUT STALL n=3 unknown=0: {findings}. Full output: /tmp/throughput-tick.latest.txt"
    )


def _decide(
    rows: tuple[str, ...],
    dispatched: dict[str, str] | None = None,
    covered_lanes: tuple[str, ...] = (),
) -> ModelStallResponderResult:
    return HandlerStallCauseResponder().handle(
        ModelStallResponderRequest(
            rows=rows, dispatched=dispatched or {}, covered_lanes=covered_lanes
        )
    )


def test_subjects_sharing_a_long_prefix_get_distinct_lane_names() -> None:
    one, two = f"floor:{LONG_PREFIX}-one", f"floor:{LONG_PREFIX}-two"
    result = _decide((_stall_row("2026-11-07T00:00:00Z", f"{one}, {two}"),))
    assert [d.cause_key for d in result.dispatches] == [one, two]
    lanes = [d.lane for d in result.dispatches]
    assert len(set(lanes)) == 2, f"two causes share one lane name: {lanes}"


def test_a_lane_of_one_long_subject_does_not_cover_a_sibling_with_the_same_prefix() -> (
    None
):
    one, two = f"floor:{LONG_PREFIX}-one", f"floor:{LONG_PREFIX}-two"
    rows = (_stall_row("2026-11-07T00:00:00Z", f"{one}, {two}"),)
    first = _decide(rows)
    lane_one = next(d.lane for d in first.dispatches if d.cause_key == one)
    again = _decide(rows, covered_lanes=(lane_one,))
    assert [d.cause_key for d in again.dispatches] == [two]
    assert {s.cause_key: s.reason for s in again.skipped} == {
        one: f"covered:{lane_one}"
    }


def test_a_lane_of_a_longer_subject_does_not_cover_its_shorter_prefix_subject() -> None:
    rows = (_stall_row("2026-11-07T00:00:00Z", "lab-headroom:h20, lab-headroom:h201"),)
    result = _decide(rows, covered_lanes=("stall-lab-headroom-h201-20261107T0000Z",))
    assert [d.cause_key for d in result.dispatches] == ["lab-headroom:h20"]


def test_a_floor_subject_is_matched_whole_not_as_a_substring_of_a_lane() -> None:
    rows = (_stall_row("2026-11-07T00:00:00Z", "floor:core, floor:omnibase_core"),)
    result = _decide(
        rows, covered_lanes=("floor-diagnose-omnibase_core-20261107T0001Z",)
    )
    assert [d.cause_key for d in result.dispatches] == ["floor:core"]
    assert {s.cause_key for s in result.skipped} == {"floor:omnibase_core"}


def test_a_replay_with_a_prior_dispatch_catches_up_in_one_step_not_one_window_per_call() -> (
    None
):
    rows = tuple(
        _stall_row(f"2026-11-0{day}T12:00:00Z", "floor:a") for day in range(2, 8)
    )
    prior = {"floor:a": "2026-11-01T00:00:00Z"}
    first = _decide(rows, dispatched=prior)
    assert [d.first_stamp for d in first.dispatches] == ["2026-11-07T12:00:00Z"]
    assert first.dispatched["floor:a"] == "2026-11-07T12:00:00Z"
    second = _decide(rows, dispatched=first.dispatched)
    assert second.dispatches == (), "the replay stepped into another historical window"
    assert {s.reason for s in second.skipped} == {"already-dispatched"}


def test_invalid_date_in_one_stall_row_is_refused_and_the_batch_goes_on() -> None:
    bad = _stall_row("2026-11-31T00:00:00Z", "floor:bad")
    good = _stall_row("2026-11-07T00:00:00Z", "floor:good")
    result = _decide((bad, good))
    assert [d.cause_key for d in result.dispatches] == ["floor:good"]
    assert [(r.stamp, r.reason) for r in result.refusals] == [
        ("2026-11-31T00:00:00Z", "invalid-calendar-date")
    ]
    assert result.stall_rows == 1


def test_invalid_date_after_a_good_row_of_the_same_key_does_not_drop_the_key() -> None:
    rows = (
        _stall_row("2026-11-07T00:00:00Z", "floor:a"),
        _stall_row("2026-02-30T00:00:00Z", "floor:a, floor:b"),
    )
    result = _decide(rows)
    assert [d.cause_key for d in result.dispatches] == ["floor:a"]
    assert len(result.refusals) == 1


def test_batch_with_an_invalid_prior_dispatch_stamp_refuses_that_key_only() -> None:
    rows = (_stall_row("2026-11-07T00:00:00Z", "floor:a, floor:b"),)
    result = _decide(rows, dispatched={"floor:a": "2026-13-45T00:00:00Z"})
    assert [d.cause_key for d in result.dispatches] == ["floor:b"]
    assert {s.cause_key: s.reason for s in result.skipped} == {
        "floor:a": "invalid-dispatched-stamp"
    }


@pytest.mark.parametrize(
    ("since", "until", "match"),
    [
        ("2026-11-07T10:00:00Z", "2026-11-07T09:59:59Z", "until must not be earlier"),
        ("2026-13-07T00:00:00Z", "", "since must be a UTC stamp"),
        ("2026-11-31T00:00:00Z", "", "since must be a UTC stamp"),
        ("2026-11-07T00:00:00Z", "2026-02-30T00:00:00Z", "until must be a UTC stamp"),
        ("2026-11-07T25:00:00Z", "", "since must be a UTC stamp"),
    ],
)
def test_idle_cost_refuses_an_inverted_range_and_an_impossible_date(
    since: str, until: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        HandlerLabIdleCost().handle(
            ModelLabIdleCostRequest(rows=(), since=since, until=until)
        )


def test_idle_cost_accepts_an_equal_since_and_until() -> None:
    result = HandlerLabIdleCost().handle(
        ModelLabIdleCostRequest(
            rows=(), since="2026-11-07T10:00:00Z", until="2026-11-07T10:00:00Z"
        )
    )
    assert (result.lane_minutes, result.rows) == (0.0, 0)
