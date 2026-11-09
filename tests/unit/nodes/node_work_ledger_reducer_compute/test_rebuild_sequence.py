# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A replayed row keeps the sequence of its sequenced copy (OMN-23194).

Golden chain: an unsequenced bootstrap row met later by its sequenced live twin is rebuilt under the twin's
sequence, so the rebuilt table equals the live table that promotes the NULL sequence. Error chain: two
sequences for one row, or one sequence for two rows, are still refused. Failing control: a rebuild that keeps
the first copy's NULL sequence (the unfixed behaviour) is named by the comparison as a ``ledger_seq`` difference.
"""

from __future__ import annotations

import pytest

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_rebuild import (
    WorkLedgerRebuild,
    WorkLedgerRebuildError,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_rebuild import (
    EnumAcceptedRowSource,
    ModelAcceptedRowEvent,
    ModelRebuildRequest,
)

pytestmark = pytest.mark.unit

LEDGER_ID = "rolling-work-ledger"
CLAIM = "2026-09-28T09:30:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work"
STATUS = "2026-09-28T10:05:00Z | STATUS | lane=alpha | ticket=OMN-1 | progress"


def _bootstrap(row: str, ordinal: int) -> ModelAcceptedRowEvent:
    return ModelAcceptedRowEvent(
        ledger_id=LEDGER_ID,
        row_id=work_ledger_row_id(row),
        raw_row=row,
        source=EnumAcceptedRowSource.BOOTSTRAP,
        legacy_ordinal=ordinal,
    )


def _live(row: str, seq: int) -> ModelAcceptedRowEvent:
    return ModelAcceptedRowEvent(
        ledger_id=LEDGER_ID,
        row_id=work_ledger_row_id(row),
        raw_row=row,
        source=EnumAcceptedRowSource.LIVE,
        ledger_seq=seq,
    )


def _rebuild(*events: ModelAcceptedRowEvent):
    return WorkLedgerRebuild().rebuild(tuple(events))


def test_golden_rebuild_takes_the_sequence_from_the_sequenced_copy() -> None:
    table = _rebuild(_bootstrap(CLAIM, 1), _bootstrap(STATUS, 2), _live(CLAIM, 7))
    by_row = {r.row_id: r.ledger_seq for r in table.rows}
    assert by_row[work_ledger_row_id(CLAIM)] == 7
    assert by_row[work_ledger_row_id(STATUS)] is None
    # The sequenced row sorts first, the still-unsequenced one after it.
    assert [r.ledger_seq for r in table.rows] == [7, None]


def test_golden_rebuild_equals_a_live_table_that_promoted_the_sequence() -> None:
    events = (_bootstrap(CLAIM, 1), _live(CLAIM, 7))
    rebuilt = _rebuild(*events)
    request = ModelRebuildRequest(events=events, live=rebuilt)
    assert WorkLedgerRebuild().handle(request).equal is True
    promoted = rebuilt.rows[0].model_copy(update={"ledger_seq": 7})
    live = rebuilt.model_copy(update={"rows": (promoted,)})
    result = WorkLedgerRebuild().handle(ModelRebuildRequest(events=events, live=live))
    assert result.equal is True
    assert result.differences == ()


def test_the_sequenced_copy_adds_no_second_fold_to_the_state() -> None:
    once = _rebuild(_bootstrap(CLAIM, 1))
    twice = _rebuild(_bootstrap(CLAIM, 1), _live(CLAIM, 7))
    assert twice.state == once.state


def test_error_rebuild_still_refuses_one_row_under_two_sequences() -> None:
    with pytest.raises(WorkLedgerRebuildError, match="accepted under two sequences"):
        _rebuild(_bootstrap(CLAIM, 1), _live(CLAIM, 7), _live(CLAIM, 8))


def test_error_rebuild_still_refuses_one_sequence_claimed_by_two_rows() -> None:
    with pytest.raises(WorkLedgerRebuildError, match="is claimed by rows"):
        _rebuild(_bootstrap(CLAIM, 1), _live(STATUS, 7), _live(CLAIM, 7))


def test_failing_control_rebuild_that_kept_the_null_sequence_is_named_by_the_comparison() -> (
    None
):
    events = (_bootstrap(CLAIM, 1), _live(CLAIM, 7))
    rebuilt = _rebuild(*events)
    unfixed = rebuilt.model_copy(
        update={"rows": (rebuilt.rows[0].model_copy(update={"ledger_seq": None}),)}
    )
    differences = WorkLedgerRebuild().compare(unfixed, rebuilt)
    assert [d.difference for d in differences] == ["ledger_seq: rebuild None, live 7"]
