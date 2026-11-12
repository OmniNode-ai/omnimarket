# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two readers of the latest-state page (OMN-23694): the in-memory default and the Postgres reader.

Golden chain: the in-memory reader folds the row events it hears into the newest row per lane or ticket and row
type (the same pure fold the projection writer applies); the Postgres reader reads the page and the watermark from
the contract-declared latest relation with every value bound. Error chain: a reader with no tracked row types answers
no entries, a source with no latest relation and a database fault are read errors that name no DSN.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg2 import sql as pgsql

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerLatestSubject,
    ModelWorkLedgerLatestCursor,
    ModelWorkLedgerLatestQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    LATEST_PAGE_COLUMNS,
    ModelWorkLedgerQuerySource,
    PostgresWorkLedgerQueryReader,
    ProtocolWorkLedgerLatestReader,
    WorkLedgerReadError,
    load_work_ledger_query_source,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).parents[4]
    / "src/omnimarket/nodes/node_work_ledger_query_effect/contract.yaml"
)
TRACKED = frozenset({"CLAIM", "TERMINAL", "STATUS", "MSG", "HOLD"})
SOURCE = ModelWorkLedgerQuerySource(
    relation="omninode_internal.work_ledger_rows",
    state_relation="omninode_internal.work_ledger_state",
    latest_relation="omninode_internal.work_ledger_latest",
    dsn_env="OMNINODE_INTERNAL_DB_URL",
    parity_receipt_lane="work-ledger-parity",
    binding_ref="omninode_runtime_service",
)
ROWS = {
    "c1": "2026-11-10T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | repo=omnimarket | est ~1 lane-hours; displaces x; (OMN-1) | work",
    "t1": "2026-11-10T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | outcome=done | done",
    "c2": "2026-11-10T11:00:00Z | CLAIM | lane=alpha | ticket=OMN-2 | repo=omnimarket | est ~1 lane-hours; displaces x; (OMN-2) | again",
    "f1": "2026-11-10T11:10:00Z | FRICTION | lane=alpha | ticket=OMN-2 | cost ~1h | slow",
}


def _heard(tracked: frozenset[str]) -> InMemoryWorkLedgerQueryReader:
    reader = InMemoryWorkLedgerQueryReader(latest_row_types=tracked)
    for row_id, raw in ROWS.items():
        assert reader.ingest({"row_id": row_id, "raw_row": raw}) is True
    return reader


def _q(**fields: object) -> ModelWorkLedgerLatestQuery:
    return ModelWorkLedgerLatestQuery.model_validate({"limit": 10, **fields})


def test_golden_the_in_memory_reader_keeps_the_newest_row_per_subject_and_type() -> (
    None
):
    reader = _heard(TRACKED)
    assert isinstance(reader, ProtocolWorkLedgerLatestReader)
    page = reader.read_latest_page(query=_q(subject_kind="lane"))
    got = [(e.subject, e.row_type, e.row_id, e.ticket, e.outcome) for e in page.entries]
    assert got == [
        ("alpha", "CLAIM", "c2", "OMN-2", None),
        ("alpha", "TERMINAL", "t1", "OMN-1", "done"),
    ]
    assert page.has_more is False
    assert page.watermark is not None


def test_golden_a_redelivered_row_changes_nothing() -> None:
    reader = _heard(TRACKED)
    before = reader.read_latest_page(query=_q())
    assert reader.ingest({"row_id": "c1", "raw_row": ROWS["c1"]}) is False
    assert reader.read_latest_page(query=_q()) == before


def test_error_chain_no_tracked_row_types_keeps_nothing_and_the_row_still_reads() -> (
    None
):
    reader = _heard(frozenset())
    assert reader.read_latest_page(query=_q()).entries == ()
    assert len(reader) == len(ROWS)


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: Any, params: tuple[object, ...]) -> None:
        if self._conn.fault is not None:
            raise self._conn.fault
        self._conn.executed.append((_render(query), params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._conn.results[len(self._conn.executed) - 1]


def _render(query: Any) -> str:
    if isinstance(query, pgsql.Composed):
        return "".join(_render(part) for part in query.seq)
    if isinstance(query, pgsql.Identifier):
        return ".".join(f'"{name}"' for name in query.strings)
    return str(query.string)


class _Conn:
    closed = False

    def __init__(
        self, results: list[list[tuple[Any, ...]]], fault: Exception | None = None
    ) -> None:
        self.results = results
        self.fault = fault
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        self.closed = True


def _pg(conn: _Conn, source: ModelWorkLedgerQuerySource = SOURCE) -> Any:
    reader = PostgresWorkLedgerQueryReader(source)
    reader._conn = conn
    return reader


def _db_row(subject: str, row_type: str, to_cell: str | None = None) -> tuple[Any, ...]:
    values: dict[str, Any] = {
        "subject_kind": "lane",
        "subject": subject,
        "row_type": row_type,
        "row_id": f"id-{subject}-{row_type}",
        "at": datetime(2026, 11, 10, 10, tzinfo=UTC),
        "lane": subject,
        "ticket": "OMN-1",
        "pr": None,
        "outcome": "done" if row_type == "TERMINAL" else None,
        "to_cell": to_cell,
    }
    return tuple(values[c] for c in LATEST_PAGE_COLUMNS)


WATERMARK = datetime(2026, 11, 11, 6, 59, tzinfo=UTC)


def test_golden_the_postgres_page_binds_every_value_and_keys_the_order() -> None:
    conn = _Conn([[(WATERMARK,)], [_db_row("alpha", "TERMINAL")]])
    after = ModelWorkLedgerLatestCursor(
        subject_kind=EnumWorkLedgerLatestSubject.LANE, subject="x", row_type="CLAIM"
    )
    since = datetime(2026, 11, 8, tzinfo=UTC)
    page = _pg(conn).read_latest_page(
        query=_q(
            after=after,
            subject_kind="lane",
            subjects=("alpha", "beta"),
            row_types=("TERMINAL",),
            since=since,
            limit=5,
        )
    )
    (watermark_sql, _), (page_sql, params) = conn.executed
    assert "max(projected_at)" in watermark_sql
    assert '"omninode_internal"."work_ledger_latest"' in page_sql
    assert "(subject_kind, subject, row_type) > (%s, %s, %s)" in page_sql
    assert "ORDER BY subject_kind, subject, row_type LIMIT %s" in page_sql
    assert "alpha" not in page_sql
    assert params == (
        "lane",
        "x",
        "CLAIM",
        "lane",
        ["alpha", "beta"],
        ["TERMINAL"],
        since,
        6,
    )
    assert page.watermark == WATERMARK
    assert page.has_more is False
    assert [(e.subject, e.row_type, e.outcome) for e in page.entries] == [
        ("alpha", "TERMINAL", "done")
    ]


def test_golden_one_entry_past_the_limit_is_has_more_and_is_not_returned() -> None:
    rows = [_db_row("a", "CLAIM"), _db_row("a", "TERMINAL"), _db_row("b", "CLAIM")]
    page = _pg(_Conn([[(WATERMARK,)], rows])).read_latest_page(query=_q(limit=2))
    assert [(e.subject, e.row_type) for e in page.entries] == [
        ("a", "CLAIM"),
        ("a", "TERMINAL"),
    ]
    assert page.has_more is True


def test_golden_the_to_cell_column_is_the_entrys_to() -> None:
    page = _pg(
        _Conn([[(WATERMARK,)], [_db_row("a", "MSG", "orchestrator")]])
    ).read_latest_page(query=_q())
    assert page.entries[0].to == "orchestrator"


def test_error_chain_a_source_with_no_latest_relation_is_a_read_error() -> None:
    source = SOURCE.model_copy(update={"latest_relation": None})
    with pytest.raises(WorkLedgerReadError, match="latest_relation is not declared"):
        _pg(_Conn([]), source).read_latest_page(query=_q())


def test_error_chain_a_database_fault_is_a_read_error_naming_no_dsn() -> None:
    conn = _Conn(
        [], fault=RuntimeError("could not connect to postgresql://u:secret@h/db")
    )
    with pytest.raises(WorkLedgerReadError) as raised:
        _pg(conn).read_latest_page(query=_q())
    assert "secret" not in str(raised.value)
    assert conn.closed is True


def test_the_contract_declares_the_latest_relation() -> None:
    source = load_work_ledger_query_source(CONTRACT)
    assert source.latest_relation == "omninode_internal.work_ledger_latest"
