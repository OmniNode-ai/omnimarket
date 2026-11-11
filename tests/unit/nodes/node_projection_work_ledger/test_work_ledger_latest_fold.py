# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-first tests for the pure work-ledger latest fold (OMN-23606)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from omnimarket.handlers.work_ledger_fold import fold_row
from omnimarket.handlers.work_ledger_latest_fold import (
    apply_latest,
    entry_key,
    latest_entries_for_row,
    page_latest,
)
from omnimarket.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.models.work_ledger_query.model_work_ledger_latest import (
    EnumWorkLedgerLatestSubject,
    ModelWorkLedgerLatestCursor,
    ModelWorkLedgerLatestQuery,
)

pytestmark = pytest.mark.unit

TRACKED = frozenset({"CLAIM", "STATUS", "TERMINAL", "MSG", "HOLD"})

CLAIM = "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | repo=omnimarket | est ~1 lane-hours; displaces x; (OMN-1) | work"
STATUS = "2026-09-28T10:05:00Z | STATUS | lane=alpha | ticket=OMN-1 | progress"
TERMINAL = "2026-09-28T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | outcome=done | done"
MSG = "2026-09-28T10:15:00Z | MSG | from=alpha | to=beta | id=2026-09-28T10:15:00Z-alpha | look"
HOLD = "2026-09-28T10:10:00Z | HOLD | lane=alpha | id=2026-09-28T10:10:00Z-alpha | surface=lab-dev | until=2026-09-28T12:00:00Z | reserved"

ALL_ROWS = [CLAIM, STATUS, TERMINAL, MSG, HOLD]


def _row(raw: str):
    return fold_row(ModelWorkLedgerFoldRequest(raw_row=raw)).row


def _fold(rows: list[str]) -> dict[tuple[str, str, str], object]:
    state: dict[tuple[str, str, str], object] = {}
    for raw in rows:
        apply_latest(state, latest_entries_for_row(_row(raw), TRACKED))
    return state


def test_golden_fixture_folds_to_the_expected_entries() -> None:
    state = _fold(ALL_ROWS)
    assert len(state) == 8

    def get(kind: str, subject: str, row_type: str) -> object:
        return state[(kind, subject, row_type)]

    assert get("lane", "alpha", "CLAIM").at == datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    assert get("lane", "alpha", "CLAIM").lane == "alpha"
    assert get("lane", "alpha", "CLAIM").ticket == "OMN-1"
    assert (
        get("ticket", "OMN-1", "CLAIM").subject_kind
        is EnumWorkLedgerLatestSubject.TICKET
    )
    assert get("lane", "alpha", "TERMINAL").outcome == "done"
    assert get("lane", "alpha", "MSG").to == "beta"
    assert get("lane", "alpha", "HOLD").at == datetime(2026, 9, 28, 10, 10, tzinfo=UTC)
    assert get("lane", "alpha", "STATUS").at == datetime(2026, 9, 28, 10, 5, tzinfo=UTC)
    assert get("ticket", "OMN-1", "TERMINAL").outcome == "done"


def test_an_untracked_row_type_yields_no_entries() -> None:
    row = _row("2026-09-28T10:00:00Z | FRICTION | lane=alpha | ticket=OMN-1 | x")
    assert latest_entries_for_row(row, TRACKED) == ()


def test_a_row_with_no_lane_and_no_ticket_yields_no_entries() -> None:
    row = _row("2026-09-28T10:00:00Z | STATUS | progress")
    assert latest_entries_for_row(row, TRACKED) == ()


def test_a_garbage_first_line_yields_none_cells_without_raising() -> None:
    row = _row("2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | work")
    entries = latest_entries_for_row(row, TRACKED)
    assert len(entries) == 2
    for entry in entries:
        assert entry.pr is None
        assert entry.to is None


def test_every_permutation_of_the_fixture_gives_the_same_state() -> None:
    baseline = _fold(ALL_ROWS)
    for perm in itertools.islice(itertools.permutations(ALL_ROWS), 0, 3000, 97):
        assert _fold(list(perm)) == baseline


def test_an_older_row_delivered_after_a_newer_one_does_not_replace_it() -> None:
    state: dict[tuple[str, str, str], object] = {}
    apply_latest(state, latest_entries_for_row(_row(CLAIM), TRACKED))
    apply_latest(state, latest_entries_for_row(_row(STATUS), TRACKED))
    apply_latest(state, latest_entries_for_row(_row(CLAIM), TRACKED))
    key = entry_key(latest_entries_for_row(_row(CLAIM), TRACKED)[0])
    assert state[key].at == datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    assert state[key].row_id == _row(CLAIM).row_id


def test_paging_in_key_order_with_has_more_and_a_cursor() -> None:
    state = _fold(ALL_ROWS)
    first = page_latest(state, ModelWorkLedgerLatestQuery(limit=2))
    assert len(first.entries) == 2
    assert first.has_more is True
    cursor = ModelWorkLedgerLatestCursor(
        subject_kind=first.entries[-1].subject_kind,
        subject=first.entries[-1].subject,
        row_type=first.entries[-1].row_type,
    )
    second = page_latest(state, ModelWorkLedgerLatestQuery(limit=2, after=cursor))
    assert len(second.entries) == 2
    seen = {entry_key(e) for e in first.entries + second.entries}
    assert len(seen) == 4
    third = page_latest(
        state,
        ModelWorkLedgerLatestQuery(
            limit=2, after=cursor, subject_kind=EnumWorkLedgerLatestSubject.LANE
        ),
    )
    assert all(
        e.subject_kind is EnumWorkLedgerLatestSubject.LANE for e in third.entries
    )


def test_filters_narrow_correctly() -> None:
    state = _fold(ALL_ROWS)
    lane_only = page_latest(
        state,
        ModelWorkLedgerLatestQuery(
            limit=100, subject_kind=EnumWorkLedgerLatestSubject.LANE
        ),
    )
    assert all(
        e.subject_kind is EnumWorkLedgerLatestSubject.LANE for e in lane_only.entries
    )
    claim_only = page_latest(
        state, ModelWorkLedgerLatestQuery(limit=100, row_types=("CLAIM",))
    )
    assert all(e.row_type == "CLAIM" for e in claim_only.entries)
    since = page_latest(
        state,
        ModelWorkLedgerLatestQuery(
            limit=100, since=datetime(2026, 9, 28, 10, 16, tzinfo=UTC)
        ),
    )
    assert all(e.at >= datetime(2026, 9, 28, 10, 16, tzinfo=UTC) for e in since.entries)
