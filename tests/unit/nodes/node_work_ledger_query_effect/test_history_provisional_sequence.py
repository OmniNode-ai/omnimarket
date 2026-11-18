# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""History completeness reads a projection-assigned (provisional) ledger_seq as unsequenced (OMN-24634).

Review of omnimarket#6306, finding 2: judge rows 1-3 with judge row 4 lost and an unsequenced live row
holding the provisional number 4 read as a complete history, because the reader took the projection's
number for the judge's. The reader now returns a provisional row with no sequence, so the history is
incomplete (the row has no judge sequence and the judge's 4 is a gap).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryRequest
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ModelWorkLedgerQuerySource,
    PostgresWorkLedgerQueryReader,
)

pytestmark = pytest.mark.unit

CUTOFF = datetime(2026, 10, 25, tzinfo=UTC)
SOURCE = "onex-ledger"
SOURCE_MODEL = ModelWorkLedgerQuerySource(
    relation="omninode_internal.work_ledger_rows",
    state_relation="omninode_internal.work_ledger_state",
    dsn_env="OMNINODE_INTERNAL_DB_URL",
    parity_receipt_lane="work-ledger-parity",
    binding_ref="omninode_runtime_service",
)


def _row(seq: int, *, provisional: bool) -> tuple[Any, ...]:
    stamp = CUTOFF - timedelta(hours=seq)
    raw = f"{stamp:%Y-%m-%dT%H:%M:%SZ} | CLAIM | lane=member | run=run-a | principal=dev-member | id=ref-{seq}"
    return (
        f"row-{seq}",
        stamp,
        "CLAIM",
        "member",
        raw,
        SOURCE,
        stamp,
        seq,
        CUTOFF - timedelta(minutes=5),
        provisional,
    )


class _Reader(PostgresWorkLedgerQueryReader):
    """The reader over a canned result in the shape the history statement returns."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        super().__init__(SOURCE_MODEL)
        self.rows = rows
        self.statements: list[str] = []

    def _query(
        self, template: str, params: tuple[object, ...]
    ) -> list[tuple[Any, ...]]:
        self.statements.append(template)
        return self.rows


def _history(reader: _Reader) -> Any:
    request = ModelWorkLedgerQueryRequest.model_validate(
        {
            "correlation_id": str(uuid4()),
            "query": "rows",
            "until": CUTOFF,
            "history": {
                "ledger_id": "rolling-work-ledger",
                "first_seq": 1,
                "last_seq": 4,
                "sources": (SOURCE,),
            },
        }
    )
    return HandlerWorkLedgerQuery(reader=reader).handle(request)


def test_provisional_row_at_the_lost_judge_number_is_not_a_complete_history() -> None:
    reader = _Reader(
        [
            _row(1, provisional=False),
            _row(2, provisional=False),
            _row(3, provisional=False),
            _row(4, provisional=True),  # judge 4 lost; the projection's own row holds 4
        ]
    )
    result = _history(reader)
    assert result.completeness.status == "incomplete"
    assert "missing-sequence" in result.completeness.reasons
    assert "sequence-gap" in result.completeness.reasons


def test_provisional_row_is_returned_with_no_sequence_and_the_statement_selects_it() -> (
    None
):
    reader = _Reader([_row(1, provisional=False), _row(2, provisional=True)])
    records = reader.read_history(
        ledger_id="rolling-work-ledger", first_seq=1, last_seq=4, until=CUTOFF
    )
    assert [r.ledger_seq for r in records] == [1, None]
    assert "ledger_seq_provisional" in reader.statements[0]
    assert "OR ledger_seq_provisional" in reader.statements[0]


def test_history_of_judge_rows_alone_is_still_complete() -> None:
    """The control: with no provisional row the same four rows read complete."""
    reader = _Reader([_row(n, provisional=False) for n in (1, 2, 3, 4)])
    result = _history(reader)
    assert result.completeness.status == "complete"
