# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: the gatherer builds phases by the plan's section 2.1."""

from __future__ import annotations

import pytest

from omnimarket.models.model_work_progress import (
    ModelWorkProgressSubject,
)
from omnimarket.nodes.node_work_progress_alert_effect.handlers.gather_lanes import (
    gather_subjects,
    parse_cells,
)
from omnimarket.nodes.node_work_progress_alert_effect.models import (
    ModelIgnoreList,
    ModelLedgerRow,
)
from tests.nodes.node_work_progress_alert_effect.support import NOW, row

NO_IGNORE = ModelIgnoreList(lanes=[], actors=[])
LOOKBACK = 86400


def gather(
    rows: list[ModelLedgerRow],
    *,
    ignore: ModelIgnoreList = NO_IGNORE,
    lookback: int = LOOKBACK,
) -> tuple[ModelWorkProgressSubject, ...]:
    return gather_subjects(rows, now=NOW, lookback_seconds=lookback, ignore=ignore)


def only(
    rows: list[ModelLedgerRow], *, ignore: ModelIgnoreList = NO_IGNORE
) -> ModelWorkProgressSubject:
    subjects = gather(rows, ignore=ignore)
    assert len(subjects) == 1
    return subjects[0]


def test_parse_cells_first_cell_of_a_key_wins() -> None:
    cells = parse_cells("t | CLAIM | lane=a | pr=1 | pr=2 | free text, no cell")
    assert cells == {"lane": "a", "pr": "1"}


def test_placement_claim_alone_is_placed() -> None:
    claim = row(600, "CLAIM", "a", host="h1", parent="orch")
    subject = only([claim])
    assert (subject.phase, subject.entering_row_id) == ("placed", claim.row_id)
    assert subject.entered_at == claim.row_ts
    assert subject.owner == "orch"


def test_ten_status_rows_without_pr_leave_claimed_at_the_first_status() -> None:
    claim = row(5000, "CLAIM", "a", host="h1")
    statuses = [row(4000 - 100 * n, "STATUS", "a", note=str(n)) for n in range(10)]
    subject = only([claim, *reversed(statuses)])
    assert subject.phase == "claimed"
    assert subject.entering_row_id == statuses[0].row_id
    assert subject.entered_at == statuses[0].row_ts


def test_first_pr_row_enters_pr_open_and_a_second_does_not_move_it() -> None:
    claim = row(5000, "CLAIM", "a", host="h1")
    status = row(4000, "STATUS", "a")
    first_pr = row(3000, "STATUS", "a", pr="repo#1")
    second_pr = row(2000, "STATUS", "a", pr="repo#2")
    subject = only([claim, status, first_pr, second_pr])
    assert subject.phase == "pr_open"
    assert subject.entering_row_id == first_pr.row_id
    assert subject.entered_at == first_pr.row_ts


def test_placement_claim_carrying_pr_enters_pr_open_at_that_claim() -> None:
    claim = row(5000, "CLAIM", "a", host="h1", pr="repo#1")
    status = row(4000, "STATUS", "a")
    subject = only([status, claim])
    assert (subject.phase, subject.entering_row_id) == ("pr_open", claim.row_id)


def test_claim_without_host_is_claimed_not_placed() -> None:
    claim = row(5000, "CLAIM", "a")
    subject = only([claim])
    assert (subject.phase, subject.entering_row_id) == ("claimed", claim.row_id)


def test_blank_pr_cell_is_not_a_pr() -> None:
    subject = only(
        [row(5000, "CLAIM", "a", host="h1"), row(4000, "STATUS", "a", pr="")]
    )
    assert subject.phase == "claimed"


def test_lane_without_a_claim_in_lookback_is_not_a_subject() -> None:
    assert gather([row(100, "STATUS", "a"), row(50, "STATUS", "a", pr="x#1")]) == ()
    assert gather([row(LOOKBACK + 1, "CLAIM", "a", host="h1")]) == ()
    assert len(gather([row(LOOKBACK, "CLAIM", "a", host="h1")])) == 1


def test_terminal_removes_the_lane_whatever_its_outcome() -> None:
    rows = [
        row(5000, "CLAIM", "a", host="h1"),
        row(100, "TERMINAL", "a", outcome="blocked"),
        row(5000, "CLAIM", "b", host="h1"),
    ]
    assert [s.lane for s in gather(rows)] == ["b"]


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_equal_stamps_select_the_smaller_row_id_in_either_order(
    order: tuple[int, int],
) -> None:
    claim = row(5000, "CLAIM", "a", host="h1")
    twins = [row(4000, "STATUS", "a", note="x"), row(4000, "STATUS", "a", note="y")]
    assert twins[0].row_ts == twins[1].row_ts
    smaller = min(twins, key=lambda r: r.row_id)
    subject = only([claim, *(twins[i] for i in order)])
    assert subject.entering_row_id == smaller.row_id


def test_two_placement_claims_keep_the_earlier_as_placed_the_later_as_claimed() -> None:
    first = row(6000, "CLAIM", "a", host="h1")
    second = row(3000, "CLAIM", "a", host="h2")
    # The later CLAIM is the claimed entry, which is the current phase; the
    # earlier one is therefore the placed entry and not the claimed one.
    for rows in ([first, second], [second, first]):
        subject = only(rows)
        assert (subject.phase, subject.entering_row_id) == ("claimed", second.row_id)
        assert subject.entered_at == second.row_ts


def test_lane_leaves_once_the_earlier_claim_ages_out_of_the_lookback() -> None:
    first = row(6000, "CLAIM", "a", host="h1")
    second = row(3000, "CLAIM", "a", host="h2")
    assert len(gather([first, second], lookback=7000)) == 1
    # The later CLAIM still lies inside the window; the lane leaves anyway.
    assert gather([first, second], lookback=5000) == ()


def test_owner_is_the_earliest_claims_parent_even_when_a_later_claim_names_another() -> (
    None
):
    first = row(6000, "CLAIM", "a", host="h1", parent="orch-1")
    second = row(3000, "CLAIM", "a", host="h2", parent="orch-2")
    assert only([second, first]).owner == "orch-1"


def test_earliest_claim_without_parent_gives_the_lane_id() -> None:
    first = row(6000, "CLAIM", "a", host="h1")
    second = row(3000, "CLAIM", "a", host="h2", parent="orch-2")
    assert only([second, first]).owner == "a"


def test_ignored_lane_id_is_not_a_subject() -> None:
    rows = [
        row(600, "CLAIM", "ctl-landing-1", host="h1"),
        row(600, "CLAIM", "b", host="h1"),
    ]
    ignore = ModelIgnoreList(lanes=["ctl-*"], actors=[])
    assert [s.lane for s in gather(rows, ignore=ignore)] == ["b"]


def test_ignored_actor_is_not_a_subject() -> None:
    rows = [
        row(600, "CLAIM", "a", host="h1", actor="script:worker"),
        row(600, "CLAIM", "b", host="h1", actor="claude:sonnet"),
    ]
    ignore = ModelIgnoreList(lanes=[], actors=["script:*"])
    assert [s.lane for s in gather(rows, ignore=ignore)] == ["b"]


def test_actor_on_any_row_of_the_lane_ignores_it() -> None:
    rows = [
        row(600, "CLAIM", "a", host="h1"),
        row(300, "STATUS", "a", actor="script:worker"),
    ]
    ignore = ModelIgnoreList(lanes=[], actors=["script:worker"])
    assert gather(rows, ignore=ignore) == ()


def test_rows_without_a_lane_or_with_the_key_separator_are_skipped() -> None:
    bad = ModelLedgerRow(
        row_id="x", row_ts=NOW, row_type="CLAIM", row_lane=None, raw_row="host=h"
    )
    piped = row(600, "CLAIM", "a|b", host="h1")
    assert gather([bad, piped]) == ()
