# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""THROUGHPUT STALL rows start one cause lane per cause key; lab-idle cost is summed (OMN-22885).

The replay fixtures are the ledger's own rows of 2026-11-07: the 59 stall MSG rows the throughput
tick wrote from 00:08:20Z to 10:30:35Z, and the 30 lab-idle FRICTION rows from 00:35:28Z to
10:36:26Z. Their tier-reasons cells, which only the lab-fill diagnosis reads, are dropped, and
the principal, machine paths and the names of two repositories are replaced
(the two running-vs-merged cause keys carry the replaced paths).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_idle_cost_compute.handlers.handler_lab_idle_cost import (
    HandlerLabIdleCost,
    last_orchestrator_start,
    sum_start,
)
from omnimarket.nodes.node_lab_idle_cost_compute.models.model_lab_idle_cost import (
    ModelLabIdleCostRequest,
)
from omnimarket.nodes.node_stall_responder_compute.handlers.handler_stall_cause_responder import (
    HandlerStallCauseResponder,
)
from omnimarket.nodes.node_stall_responder_compute.models.model_stall_responder import (
    ModelStallResponderRequest,
    ModelStallResponderResult,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STALL_ROWS = tuple(
    (FIXTURES / "stall_msg_rows_2026-11-07.txt").read_text().splitlines()
)
IDLE_ROWS = tuple(
    (FIXTURES / "lab_idle_friction_rows_2026-11-07.txt").read_text().splitlines()
)

# Every complete finding the 59 rows name, in the order the rows first name it.
CAUSE_KEYS = (
    "floor:omnibase_core",
    "floor:omnibase_infra",
    "floor:omnibase_spi",
    "repo-status:omnibase_infra",
    "running-vs-merged:dispatch-venv:h101:/opt/venv-a/omniclaude/.venv",
    "lab-headroom:h101",
    "lab-headroom:h201",
    "lab-headroom:h053",
    "floor:omniclaude",
    "lab-headroom:h106",
    "floor:omnimarket",
    "repo-status:omnimarket",
    "repo-status:omniclaude",
    "merges",
    "floor:omnibase_beta",
    "floor:omnibase_alpha",
    "running-vs-merged:dispatch-venv:h200:/opt/venv-b/omniclaude/.venv",
    "floor:omnidash",
    "repo-status:omnibase_alpha",
)
TICK_OWNED = (
    "dispatch:floor-diagnose-omnibase_spi-20261106T2244Z",
    "dispatch:infra-mq-0830",
)


def _stall_row(
    stamp: str,
    findings: str,
    *,
    cut: bool = False,
    sender: str = "launchd-throughput-tick",
) -> str:
    tail = "...." if cut else "."
    return (
        f"{stamp} | MSG | from={sender} | principal=p | to=orchestrator-1 | ticket=OMN-20800 | "
        f"req=r | via=bus:h200 | throughput-tick is not OK: THROUGHPUT STALL n=3 unknown=0: "
        f"{findings}{tail} Full output: /tmp/throughput-tick.latest.txt"
    )


def _decide(
    rows: tuple[str, ...],
    dispatched: dict[str, str] | None = None,
    covered_lanes: tuple[str, ...] = (),
    redispatch_after_hours: float = 24.0,
) -> ModelStallResponderResult:
    return HandlerStallCauseResponder().handle(
        ModelStallResponderRequest(
            rows=rows,
            dispatched=dispatched or {},
            covered_lanes=covered_lanes,
            redispatch_after_hours=redispatch_after_hours,
        )
    )


def test_replay_of_the_59_rows_dispatches_once_per_distinct_cause_key() -> None:
    assert len(STALL_ROWS) == 59
    result = _decide(STALL_ROWS)
    assert result.stall_rows == 59
    assert result.unreadable_rows == 0
    keys = tuple(d.cause_key for d in result.dispatches)
    assert len(keys) == len(set(keys)), "a cause key dispatched twice"
    assert set(keys) == set(CAUSE_KEYS)
    assert tuple(sorted(keys)) == tuple(sorted(CAUSE_KEYS))
    assert {s.cause_key: s.reason for s in result.skipped} == dict.fromkeys(
        TICK_OWNED, "tick-owns-relaunch"
    )
    lanes = [d.lane for d in result.dispatches]
    assert len(lanes) == len(set(lanes))
    assert all(lane.startswith("stall-") for lane in lanes)
    core = next(d for d in result.dispatches if d.cause_key == "floor:omnibase_core")
    assert (core.occurrences, core.first_stamp) == (58, "2026-11-07T00:08:20Z")
    assert core.lane == "stall-floor-omnibase-core-20261107T0008Z"


def test_every_stall_row_of_the_night_is_cut_and_its_last_finding_is_not_dispatched() -> (
    None
):
    result = _decide(STALL_ROWS)
    assert result.truncated_findings == 59
    for d in result.dispatches:
        assert d.cause_key in CAUSE_KEYS, f"fragment dispatched: {d.cause_key}"
    assert not any(
        d.cause_key in ("repo-status:omnibas", "lab-headroom:h0")
        for d in result.dispatches
    )


def test_replaying_the_same_rows_again_dispatches_nothing_and_a_duplicate_row_counts_once() -> (
    None
):
    first = _decide(STALL_ROWS)
    again = _decide(STALL_ROWS + STALL_ROWS, dispatched=first.dispatched)
    assert again.dispatches == ()
    assert {s.reason for s in again.skipped} == {
        "already-dispatched",
        "tick-owns-relaunch",
    }
    doubled = _decide(STALL_ROWS + STALL_ROWS)
    assert doubled.stall_rows == 59
    assert [d.cause_key for d in doubled.dispatches] == [
        d.cause_key for d in first.dispatches
    ]


def test_a_key_dispatches_again_only_when_a_stall_row_names_it_after_the_window() -> (
    None
):
    rows = (
        _stall_row("2026-11-07T00:00:00Z", "floor:a, floor:b"),
        _stall_row("2026-11-07T23:00:00Z", "floor:a, floor:b"),
        _stall_row("2026-11-08T01:00:00Z", "floor:a, floor:b"),
    )
    first = _decide(rows[:2])
    assert [d.cause_key for d in first.dispatches] == ["floor:a", "floor:b"]
    inside = _decide(rows[:2], dispatched=first.dispatched)
    assert inside.dispatches == ()
    assert {s.reason for s in inside.skipped} == {"already-dispatched"}
    later = _decide(rows, dispatched=first.dispatched)
    assert [d.first_stamp for d in later.dispatches] == ["2026-11-08T01:00:00Z"] * 2
    assert later.dispatched["floor:a"] == "2026-11-08T01:00:00Z"
    short = _decide(rows[:2], dispatched=first.dispatched, redispatch_after_hours=12)
    assert len(short.dispatches) == 2


def test_a_live_lane_of_the_class_covers_the_key() -> None:
    rows = (
        _stall_row(
            "2026-11-07T00:00:00Z",
            "floor:omnibase_spi, lab-headroom:h201, floor:omniclaude",
        ),
    )
    result = _decide(
        rows,
        covered_lanes=(
            "floor-diagnose-omnibase_spi-20261107T0001Z",
            "stall-lab-headroom-h201-20261107T0000Z",
            "fill-ticket-OMN-1-h201",
        ),
    )
    assert [d.cause_key for d in result.dispatches] == ["floor:omniclaude"]
    reasons = {s.cause_key: s.reason for s in result.skipped}
    assert reasons == {
        "floor:omnibase_spi": "covered:floor-diagnose-omnibase_spi-20261107T0001Z",
        "lab-headroom:h201": "covered:stall-lab-headroom-h201-20261107T0000Z",
    }
    assert [d.cause_key for d in _decide(rows).dispatches] == [
        "floor:omnibase_spi",
        "lab-headroom:h201",
        "floor:omniclaude",
    ]


def test_a_cut_final_finding_is_dropped_and_an_uncut_one_is_kept() -> None:
    cut = _decide((_stall_row("2026-11-07T00:00:00Z", "floor:a, repo-sta", cut=True),))
    assert [d.cause_key for d in cut.dispatches] == ["floor:a"]
    assert cut.truncated_findings == 1
    whole = _decide((_stall_row("2026-11-07T00:00:00Z", "floor:a, repo-status:b"),))
    assert [d.cause_key for d in whole.dispatches] == ["floor:a", "repo-status:b"]
    assert whole.truncated_findings == 0


def test_only_the_tick_s_stall_msg_rows_count() -> None:
    other_sender = _stall_row(
        "2026-11-07T00:00:00Z", "floor:a", sender="orchestrator-1"
    )
    friction = (
        "2026-11-07T00:00:00Z | FRICTION | lane=x | ticket=OMN-1 | "
        "THROUGHPUT STALL n=1 unknown=0: floor:a. Full output: /x"
    )
    ok_tick = _stall_row("2026-11-07T00:00:00Z", "floor:a").replace(
        "THROUGHPUT STALL", "THROUGHPUT OK"
    )
    result = _decide((other_sender, friction, ok_tick, "not a ledger row"))
    assert (result.stall_rows, result.dispatches, result.skipped) == (0, (), ())
    assert _decide((_stall_row("2026-11-07T00:00:00Z", "floor:a"),)).stall_rows == 1


def test_a_stall_row_in_a_shape_the_responder_cannot_read_is_counted_unreadable() -> (
    None
):
    odd = _stall_row("2026-11-07T00:00:00Z", "floor:a").replace(
        " Full output:", " see file:"
    )
    result = _decide((odd,))
    assert result.unreadable_rows == 1
    assert result.dispatches == ()


def test_a_finding_with_no_cause_class_starts_nothing() -> None:
    result = _decide(
        (_stall_row("2026-11-07T00:00:00Z", ":nothing, 9lives:x, floor:a"),)
    )
    assert [d.cause_key for d in result.dispatches] == ["floor:a"]
    assert {s.reason for s in result.skipped} == {"no-cause-class"}


def test_the_lab_idle_sum_of_the_30_rows_is_7100_lane_minutes() -> None:
    assert len(IDLE_ROWS) == 30
    result = HandlerLabIdleCost().handle(
        ModelLabIdleCostRequest(rows=IDLE_ROWS, since="2026-11-07T00:35:28Z")
    )
    assert (result.lane_minutes, result.rows, result.unreadable) == (7100.0, 30, 0)
    assert (
        result.segment == "lab-idle 7,100 lane-min (30 rows since 2026-11-07T00:35:28Z)"
    )


def test_the_sum_counts_from_the_start_and_to_the_end_inclusive_and_once_per_row() -> (
    None
):
    handler = HandlerLabIdleCost()
    later = handler.handle(
        ModelLabIdleCostRequest(rows=IDLE_ROWS, since="2026-11-07T10:00:00Z")
    )
    assert (later.lane_minutes, later.rows) == (500.0, 2)
    none = handler.handle(
        ModelLabIdleCostRequest(rows=IDLE_ROWS, since="2026-11-07T12:00:00Z")
    )
    assert (none.lane_minutes, none.rows) == (0.0, 0)
    assert none.segment == "lab-idle 0 lane-min (0 rows since 2026-11-07T12:00:00Z)"
    bounded = handler.handle(
        ModelLabIdleCostRequest(
            rows=IDLE_ROWS, since="2026-11-07T00:35:28Z", until="2026-11-07T00:35:28Z"
        )
    )
    assert (bounded.lane_minutes, bounded.rows) == (280.0, 1)
    doubled = handler.handle(
        ModelLabIdleCostRequest(
            rows=IDLE_ROWS + IDLE_ROWS, since="2026-11-07T00:35:28Z"
        )
    )
    assert doubled.lane_minutes == 7100.0


def test_other_friction_classes_and_unreadable_costs_are_not_summed_as_zero() -> None:
    other = IDLE_ROWS[0].replace("class=lab-idle", "class=placement-share")
    broken = re.sub(r"est \d+ lane-minutes", "est unknown lane-minutes", IDLE_ROWS[1])
    hours = re.sub(r"est (\d+) lane-minutes", r"est \1 lane-hours", IDLE_ROWS[2])
    assert "est unknown" in broken
    assert "lane-hours" in hours
    assert "placement-share" in other
    result = HandlerLabIdleCost().handle(
        ModelLabIdleCostRequest(
            rows=(other, broken, hours), since="2026-11-07T00:00:00Z"
        )
    )
    assert result.rows == 1
    assert result.unreadable == 1
    found = re.search(r"est (\d+) lane-hours", hours)
    assert found is not None
    assert result.lane_minutes == 60 * float(found.group(1))
    assert result.segment.endswith(", 1 unreadable)")


def test_a_malformed_start_is_refused() -> None:
    with pytest.raises(ValueError, match="since must be a UTC stamp"):
        HandlerLabIdleCost().handle(
            ModelLabIdleCostRequest(rows=IDLE_ROWS, since="yesterday")
        )


def test_the_last_start_is_the_newest_orchestrator_claim() -> None:
    rows = (
        "2026-11-07T01:00:00Z | CLAIM | lane=orchestrator-aaaa | ticket=OMN-1 | x",
        "2026-11-07T03:00:00Z | CLAIM | lane=fill-ticket-OMN-1 | ticket=OMN-1 | x",
        "2026-11-07T02:00:00Z | CLAIM | lane=orchestrator-bbbb | ticket=OMN-1 | x",
    )
    assert last_orchestrator_start(rows) == "2026-11-07T02:00:00Z"
    assert last_orchestrator_start(rows[1:2]) is None
    assert sum_start(rows) == "2026-11-07T02:00:00Z"
    assert sum_start(rows[1:2]) == "2026-11-07T03:00:00Z"
    assert sum_start(("# no rows",)) is None
