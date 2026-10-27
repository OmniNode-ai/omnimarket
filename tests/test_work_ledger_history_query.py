# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""History acquisition preserves source order and refuses incomplete evidence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)

pytestmark = pytest.mark.unit
CUTOFF = datetime(2026, 10, 25, tzinfo=UTC)
SOURCES = ("archive/2026-10-23", "archive/2026-10-24")


def record(
    seq: int, *, lane: str = "member", run: str = "run-a"
) -> ModelWorkLedgerRowRecord:
    # Writer stamps run backwards; only the judge's sequence orders history.
    stamp = CUTOFF - timedelta(hours=seq)
    raw = f"{stamp:%Y-%m-%dT%H:%M:%SZ} | CLAIM | lane={lane} | run={run} | principal=dev-member | id=ref-{seq}"
    return ModelWorkLedgerRowRecord(
        row_id=f"row-{seq}",
        row_ts=stamp,
        row_type="CLAIM",
        row_lane=lane,
        text=raw,
        raw_row=raw,
        source=SOURCES[(seq - 1) // 2],
        ledger_seq=seq,
        ingested_at=CUTOFF - timedelta(minutes=5),
    )


def request(**overrides: object) -> ModelWorkLedgerQueryRequest:
    return ModelWorkLedgerQueryRequest.model_validate(
        {
            "correlation_id": str(uuid4()),
            "query": "rows",
            "until": CUTOFF,
            "history": {
                "ledger_id": "rolling-work-ledger",
                "first_seq": 1,
                "last_seq": 4,
                "sources": SOURCES,
            },
            **overrides,
        }
    )


class Reader:
    def __init__(self, rows: tuple[ModelWorkLedgerRowRecord, ...]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, int, int, datetime]] = []

    def read_history(
        self, *, ledger_id: str, first_seq: int, last_seq: int, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        self.calls.append((ledger_id, first_seq, last_seq, until))
        return self.rows


def query(rows: tuple[ModelWorkLedgerRowRecord, ...], **kwargs: object):
    reader = Reader(rows)
    result = HandlerWorkLedgerQuery(reader=reader).handle(request(**kwargs))
    assert reader.calls == [("rolling-work-ledger", 1, 4, CUTOFF)]
    return result


def test_history_returns_both_rolls_in_canonical_order_with_authoritative_time() -> (
    None
):
    rows = tuple(record(seq) for seq in (4, 2, 1, 3))
    result = query(rows)
    assert result.error is None
    assert [r.ledger_seq for r in result.rows] == [1, 2, 3, 4]
    assert [r.raw_row for r in result.rows] == [record(i).raw_row for i in range(1, 5)]
    assert all(r.ingested_at is not None for r in result.rows)
    assert result.completeness.status == "complete"
    assert result.completeness.time_basis == "database-ingest"
    assert result.completeness.last_seq == 4


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"lanes": ["other"]}, [2]),
        ({"run": "run-b"}, [2]),
        ({"row_ref": "ref-2"}, [2]),
        ({"row_ref": "row-2"}, [2]),
        ({"principal": "dev-other"}, []),
    ],
)
def test_history_discovery_filters(
    filters: dict[str, object], expected: list[int]
) -> None:
    rows = (record(1), record(2, lane="other", run="run-b"), record(3), record(4))
    result = query(rows, **filters)
    assert [r.ledger_seq for r in result.rows] == expected
    assert result.completeness.status == "complete"


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ((record(1), record(3), record(4)), "sequence-gap"),
        ((record(1), record(2)), "missing-source"),
        ((record(1), record(2), record(2), record(3), record(4)), "duplicate-sequence"),
        (
            (
                record(1).model_copy(update={"ingested_at": None}),
                record(2),
                record(3),
                record(4),
            ),
            "missing-ingest-time",
        ),
        (
            (
                record(1).model_copy(update={"ledger_seq": None}),
                record(2),
                record(3),
                record(4),
            ),
            "missing-sequence",
        ),
        (
            (
                record(1).model_copy(update={"ingested_at": CUTOFF}),
                record(2),
                record(3),
                record(4),
            ),
            "sequence-gap",
        ),
    ],
)
def test_history_never_calls_incomplete_evidence_complete(rows, reason: str) -> None:
    result = query(rows)
    assert result.completeness.status == "incomplete"
    assert reason in result.completeness.reasons


def test_history_pagination_binds_cursor_to_cutoff_range_and_filters() -> None:
    rows = tuple(record(i) for i in range(1, 5))
    first = query(rows, limit=2)
    assert [r.ledger_seq for r in first.rows] == [1, 2]
    assert first.completeness.status == "incomplete"
    assert "truncated" in first.completeness.reasons
    assert first.next_cursor is not None
    second = query(rows, limit=2, cursor=first.next_cursor)
    assert [r.ledger_seq for r in second.rows] == [3, 4]
    assert second.next_cursor is None
    assert second.completeness.status == "complete"
    changed = query(rows, limit=2, cursor=first.next_cursor, run="other")
    assert changed.error is not None
    assert not changed.rows


def test_history_cursor_refuses_a_changed_source_snapshot() -> None:
    rows = tuple(record(i) for i in range(1, 5))
    first = query(rows, limit=2)
    changed = (
        rows[0].model_copy(update={"raw_row": rows[0].raw_row + " changed"}),
        *rows[1:],
    )
    result = query(changed, limit=2, cursor=first.next_cursor)
    assert result.error is not None
    assert not result.rows
    assert result.completeness is None


def test_history_filters_cannot_hide_source_gaps() -> None:
    result = query((record(1), record(3), record(4)), principal="dev-other")
    assert result.rows == ()
    assert result.completeness.status == "incomplete"
    assert "sequence-gap" in result.completeness.reasons


def test_volatile_bus_history_is_explicitly_unavailable() -> None:
    from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
        InMemoryWorkLedgerQueryReader,
    )

    result = HandlerWorkLedgerQuery(reader=InMemoryWorkLedgerQueryReader()).handle(
        request()
    )
    assert result.error is not None
    assert "no authoritative retained history" in result.error
    assert result.rows == ()
    assert result.completeness is None


def test_history_reference_expansion_keeps_cross_principal_closure_lists() -> None:
    rows = [record(i) for i in range(1, 5)]
    raw = "2026-10-24T23:00:00Z | TERMINAL | lane=recovery | principal=dev-executor | closes-CLAIM=ref-1,ref-3"
    rows[3] = rows[3].model_copy(
        update={"text": raw, "raw_row": raw, "row_type": "TERMINAL"}
    )
    result = query(tuple(rows), row_ref="ref-1")
    assert [row.ledger_seq for row in result.rows] == [1, 4]
    assert result.completeness.status == "complete"


@pytest.mark.parametrize(
    "text",
    [
        "> 2026-10-24T23:00:00Z | CLAIM | lane=quoted",
        '<!-- metadata "2026-10-24T23:00:00Z | TERMINAL" -->',
        "2026-10-24T23:00:00Z | UNKNOWN | lane=member",
    ],
)
def test_history_invalid_or_quoted_source_rows_are_incomplete(text: str) -> None:
    rows = (
        record(1).model_copy(update={"text": text, "raw_row": text}),
        record(2),
        record(3),
        record(4),
    )
    result = query(rows)
    assert result.completeness.status == "incomplete"
    assert "invalid-source-row" in result.completeness.reasons


def test_history_query_dispatches_through_the_runtime_contract() -> None:
    import asyncio

    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryResult

    contract = (
        Path(__file__).parents[1]
        / "src/omnimarket/nodes/node_work_ledger_query_effect/contract.yaml"
    )
    manifest = discover_contracts_from_paths([contract])
    assert not manifest.errors
    route = manifest.contracts[0].handler_routing.handlers[0]
    node = HandlerWorkLedgerQuery(reader=Reader(tuple(record(i) for i in range(1, 5))))
    callback = _make_dispatch_callback(node, event_model=route.event_model)
    payload = request()
    dispatch = asyncio.run(
        callback(
            {
                "payload": payload.model_dump(mode="json"),
                "correlation_id": str(payload.correlation_id),
            }
        )
    )
    terminal = ModelWorkLedgerQueryResult.model_validate(dispatch.output_events[0])
    assert terminal.completeness.status == "complete"
    assert [row.ledger_seq for row in terminal.rows] == [1, 2, 3, 4]


def test_legacy_rows_do_not_get_fabricated_authority() -> None:
    row = record(1).model_dump(exclude={"ledger_seq", "ingested_at"})
    legacy = ModelWorkLedgerRowRecord.model_validate(row)
    assert legacy.ledger_seq is None
    assert legacy.ingested_at is None


@pytest.mark.parametrize(
    "changes",
    [
        {"history": None, "cursor": "invalid"},
        {
            "history": {
                "ledger_id": "rolling-work-ledger",
                "first_seq": 4,
                "last_seq": 1,
            }
        },
        {"since": CUTOFF - timedelta(days=2)},
        {"until": CUTOFF.replace(tzinfo=None)},
    ],
)
def test_history_rejects_ambiguous_ranges(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(**changes)


def test_ingest_migration_does_not_backfill_historical_authority() -> None:
    path = (
        Path(__file__).parents[1]
        / "src/omnimarket/nodes/node_projection_work_ledger/migrations/0006_work_ledger_ingested_at.sql"
    )
    sql = path.read_text().lower()
    assert "add column if not exists ingested_at timestamptz" in sql
    assert "alter column ingested_at set default clock_timestamp()" in sql
    assert "update " not in sql
    assert "row_ts" not in sql
