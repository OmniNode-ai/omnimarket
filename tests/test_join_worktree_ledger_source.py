# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the worktree reconcile's projection ledger source against the real work-ledger query node.

The reconcile reads `rows` answers (rows, freshness, parity with `exact_kinds`) that
`HandlerWorkLedgerQuery` writes. Here the real handler answers over the in-memory
reader, and its answer crosses the same JSON boundary the bus carries, so this test
fails when either side changes the shape of what they share (the request, the rows
answer, the parity day, the parity receipt row).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand
from omnimarket.handlers.handler_ledger_claims import owner_finished
from omnimarket.models.model_worktree_ledger_projection import (
    ModelLedgerProjectionSettings,
)
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal
from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryRequest
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_ledger_projection import (
    ProjectionLedgerReader,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
TREE = Path("/w/OMN-5/repo")
ROOT = Path("/w")
CLAIM = "2026-11-14T09:00:00Z | CLAIM | lane=lane-a | ticket=OMN-5 | worktree=omni_worktrees/OMN-5/repo"
DONE = "2026-11-14T10:00:00Z | TERMINAL | lane=lane-a | ticket=OMN-5 | outcome=done | worktree=removed:/w/OMN-5/repo"
KEPT = "2026-11-14T10:00:00Z | TERMINAL | lane=lane-a | ticket=OMN-5 | outcome=partial | worktree=kept:/w/OMN-5/repo:x"
ACTIVITY = "2026-11-15T10:00:00Z | CLAIM | lane=lane-b | ticket=OMN-9 | worktree=omni_worktrees/OMN-9/repo"


def _receipt(day: str, written: str, kinds: str) -> str:
    """The daily parity receipt row, shaped like the one the work-ledger-parity writer appends."""
    return (
        f"{written} | STATUS | lane=work-ledger-parity | actor=script:work-ledger-parity | model=none | "
        f"day={day} | file_rows=10 | projection_rows=10 | missing=0 | extra=0 | exact=yes | "
        f"exact_kinds={kinds} | Work-ledger parity receipt for UTC day {day}"
    )


ALL_YES = "ACK:yes,CLAIM:yes,RELEASE:yes,STATUS:yes,TERMINAL:yes"
TERMINAL_NO = "ACK:yes,CLAIM:yes,RELEASE:yes,STATUS:yes,TERMINAL:no"


def _store(rows: list[str], *, receipts: dict[str, str]) -> HandlerWorkLedgerQuery:
    reader = InMemoryWorkLedgerQueryReader(
        parity_receipt_lane="work-ledger-parity", now=lambda: NOW
    )
    written = {"2026-11-12": "13", "2026-11-13": "14", "2026-11-14": "15"}
    texts = rows + [
        _receipt(day, f"2026-11-{written[day]}T05:00:00Z", kinds)
        for day, kinds in receipts.items()
    ]
    for index, text in enumerate(texts):
        assert reader.ingest({"raw_row": text, "row_id": f"row-{index}"})
    return HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW)


def _reader(handler: HandlerWorkLedgerQuery) -> ProjectionLedgerReader:
    def call(request: BaseModel, correlation_id: UUID) -> ModelScmTerminal:
        assert isinstance(request, ModelWorkLedgerQueryRequest)
        result = handler.handle(request)
        return ModelScmTerminal(
            receipt=ModelScmBusReceipt(
                correlation_id=correlation_id,
                command_topic="onex.cmd.omnimarket.work-ledger-query-requested.v1",
                command_envelope_id=uuid4(),
                terminal_topic="onex.evt.omnimarket.work-ledger-query-completed.v1",
            ),
            failed=False,
            payload=result.model_dump(mode="json"),
        )

    return ProjectionLedgerReader(call=call)


def _command() -> ModelWorktreeReconcileCommand:
    return ModelWorktreeReconcileCommand(
        host="lab-host",
        roots=("/w",),
        correlation_id=uuid4(),
        ledger_source="projection",
        ledger_projection=ModelLedgerProjectionSettings(
            bus_lane="dev", timeout_seconds=30.0, page_size=2
        ),
    )


FULL = {"2026-11-12": ALL_YES, "2026-11-13": ALL_YES, "2026-11-14": ALL_YES}


def test_complete_parity_lets_a_finished_owner_vouch_for_its_tree() -> None:
    handler = _store([CLAIM, DONE, ACTIVITY], receipts=FULL)
    read = _reader(handler).read(_command(), NOW)
    assert read is not None
    assert read.reason == ""
    assert read.fresh is True
    assert owner_finished(TREE, ROOT, read) is True


def test_a_terminal_kind_the_parity_marks_incomplete_leaves_the_ledger_unread() -> None:
    receipts = FULL | {"2026-11-14": TERMINAL_NO}
    handler = _store([CLAIM, DONE, ACTIVITY], receipts=receipts)
    read = _reader(handler).read(_command(), NOW)
    assert read is not None
    assert read.fresh is False
    assert read.reason == "projection_incomplete:TERMINAL@2026-11-14"
    assert owner_finished(TREE, ROOT, read) is False


def test_a_terminal_that_keeps_the_worktree_does_not_finish_the_owner() -> None:
    handler = _store([CLAIM, KEPT, ACTIVITY], receipts=FULL)
    read = _reader(handler).read(_command(), NOW)
    assert read is not None
    assert read.fresh is True
    assert owner_finished(TREE, ROOT, read) is False


def test_a_day_with_no_receipt_before_a_measured_day_is_a_hole() -> None:
    receipts = {"2026-11-12": ALL_YES, "2026-11-14": ALL_YES}
    handler = _store([CLAIM, DONE, ACTIVITY], receipts=receipts)
    read = _reader(handler).read(_command(), NOW)
    assert read is not None
    assert read.reason == "projection_unmeasured"


def test_a_window_larger_than_a_page_is_followed_through_its_cursor() -> None:
    handler = _store([CLAIM, DONE, ACTIVITY], receipts=FULL)
    read = _reader(handler).read(_command(), NOW)  # page_size 2, three rows read
    assert read is not None
    assert read.finished_claims == (CLAIM,)
    assert ACTIVITY in read.open_claims
