# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres state reader of the point queries, over a recording connection
(OMN-21948). Every statement reads the is_open column of the contract-declared
state relation and binds its values; a database error is a read error."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg2 import sql as pgsql

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerStateCursor,
    ModelWorkLedgerStateQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    STATE_PAGE_COLUMNS,
    ModelWorkLedgerQuerySource,
    PostgresWorkLedgerQueryReader,
    WorkLedgerReadError,
    load_work_ledger_query_source,
)

pytestmark = pytest.mark.unit

K = EnumWorkLedgerQueryKind
NOW = datetime(2026, 11, 2, 3, tzinfo=UTC)
CONTRACT = (
    Path(__file__).parents[4]
    / "src/omnimarket/nodes/node_work_ledger_query_effect/contract.yaml"
)
SOURCE = ModelWorkLedgerQuerySource(
    relation="omninode_internal.work_ledger_rows",
    state_relation="omninode_internal.work_ledger_state",
    dsn_env="OMNINODE_INTERNAL_DB_URL",
    parity_receipt_lane="work-ledger-parity",
    binding_ref="omninode_runtime_service",
)


def _render(query: Any) -> str:
    """Compose the statement text without a live connection."""
    if isinstance(query, pgsql.Composed):
        return "".join(_render(part) for part in query.seq)
    if isinstance(query, pgsql.Identifier):
        return ".".join(f'"{name}"' for name in query.strings)
    return str(query.string)


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
        return self._conn.result


class _Conn:
    closed = False

    def __init__(
        self, result: list[tuple[Any, ...]], fault: Exception | None = None
    ) -> None:
        self.result = result
        self.fault = fault
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        self.closed = True


def _reader(conn: _Conn, source: ModelWorkLedgerQuerySource = SOURCE) -> Any:
    reader = PostgresWorkLedgerQueryReader(source)
    reader._conn = conn
    return reader


def _query(
    point: EnumWorkLedgerQueryKind, **fields: object
) -> ModelWorkLedgerStateQuery:
    return ModelWorkLedgerStateQuery.model_validate(
        {"point": point, "now": NOW, "limit": 10, **fields}
    )


def _meta_only() -> tuple[Any, ...]:
    values: dict[str, Any] = dict.fromkeys(STATE_PAGE_COLUMNS)
    values.update(
        watermark=datetime(2026, 11, 2, 2, 59, tzinfo=UTC),
        ledger_newest=datetime(2026, 11, 2, 2, 58, tzinfo=UTC),
        total=0,
        released=0,
        expired=0,
    )
    return tuple(values[c] for c in STATE_PAGE_COLUMNS)


def _with_entity(**fields: Any) -> tuple[Any, ...]:
    values: dict[str, Any] = dict(zip(STATE_PAGE_COLUMNS, _meta_only(), strict=True))
    values.update(
        entity_key="hold:h1",
        kind="hold",
        lane="orch",
        scope_to="a,b",
        opened_at=datetime(2026, 11, 1, tzinfo=UTC),
        row_id="r1",
        row_ts=datetime(2026, 11, 1, tzinfo=UTC),
        row_type="HOLD",
        raw_row="2026-11-01T00:00:00Z | HOLD | lane=orch | id=h1",
        source="s",
        projected_at=datetime(2026, 11, 1, tzinfo=UTC),
        total=1,
    )
    values.update(fields)
    return tuple(values[c] for c in STATE_PAGE_COLUMNS)


def test_point_query_contract_declares_the_state_relation() -> None:
    source = load_work_ledger_query_source(CONTRACT)
    assert source.state_relation == "omninode_internal.work_ledger_state"


@pytest.mark.parametrize("point", [K.HOLDS, K.INBOX, K.OPEN_CLAIMS, K.OPEN_RULINGS])
def test_point_query_statement_reads_is_open_from_the_state_relation(
    point: EnumWorkLedgerQueryKind,
) -> None:
    conn = _Conn([_meta_only()])
    page = _reader(conn).read_state_page(
        query=_query(point, lanes=("lane-x",), ticket="OMN-1")
    )
    sql, params = conn.executed[0]
    assert '"omninode_internal"."work_ledger_state"' in sql
    assert "is_open" in sql
    assert "ORDER BY" in sql
    assert "opened_at" in sql
    assert "LIMIT" in sql
    assert 11 in params  # limit + 1 to learn whether more follow
    assert page.entities == ()
    assert page.watermark == datetime(2026, 11, 2, 2, 59, tzinfo=UTC)
    assert page.has_more is False


def test_point_query_statement_binds_values_never_interpolates() -> None:
    conn = _Conn([_meta_only()])
    _reader(conn).read_state_page(
        query=_query(K.HOLDS, repo="x'; DROP TABLE y; --", pr="r#1", to="lane-x")
    )
    sql, params = conn.executed[0]
    assert "DROP TABLE" not in sql
    assert "x'; DROP TABLE y; --" in params


def test_point_query_cursor_adds_a_keyset_predicate() -> None:
    conn = _Conn([_meta_only()])
    after = ModelWorkLedgerStateCursor(
        opened_at=datetime(2026, 11, 1, tzinfo=UTC), entity_key="hold:h0"
    )
    _reader(conn).read_state_page(query=_query(K.HOLDS, after=after))
    sql, params = conn.executed[0]
    assert "(s.opened_at, s.entity_key) >" in sql
    assert "hold:h0" in params


def test_point_query_one_extra_row_means_more_follow_and_is_not_returned() -> None:
    rows = [_with_entity(entity_key="hold:h1"), _with_entity(entity_key="hold:h2")]
    conn = _Conn(rows)
    page = _reader(conn).read_state_page(
        query=ModelWorkLedgerStateQuery(point=K.HOLDS, now=NOW, limit=1)
    )
    assert [e.entity_key for e in page.entities] == ["hold:h1"]
    assert page.has_more is True


def test_point_query_open_claims_statement_aggregates_the_lane_activity() -> None:
    conn = _Conn(
        [
            _with_entity(
                entity_key="claim:l",
                kind="claim",
                lane="l",
                act_rows=3,
                act_newest_ts=datetime(2026, 11, 2, tzinfo=UTC),
                act_newest_kind="STATUS",
                act_lease_rows=["x | lease=L1 | y"],
            )
        ]
    )
    page = _reader(conn).read_state_page(query=_query(K.OPEN_CLAIMS))
    sql, _ = conn.executed[0]
    assert "row_lane" in sql
    activity = page.entities[0].activity
    assert activity is not None
    assert (activity.rows, activity.newest_row_kind, activity.leases) == (
        3,
        "STATUS",
        ("L1",),
    )


def test_point_query_entity_without_its_opened_row_is_a_read_error() -> None:
    conn = _Conn([_with_entity(row_id=None, raw_row=None, row_ts=None)])
    with pytest.raises(WorkLedgerReadError, match="hold:h1"):
        _reader(conn).read_state_page(query=_query(K.HOLDS))


def test_fault_point_query_database_error_is_a_read_error_and_closes() -> None:
    conn = _Conn([], fault=RuntimeError("server closed the connection"))
    with pytest.raises(WorkLedgerReadError, match="RuntimeError"):
        _reader(conn).read_state_page(query=_query(K.HOLDS))
    assert conn.closed


def test_fault_point_query_without_a_declared_state_relation_is_a_read_error() -> None:
    source = SOURCE.model_copy(update={"state_relation": None})
    conn = _Conn([_meta_only()])
    with pytest.raises(WorkLedgerReadError, match="state_relation"):
        _reader(conn, source).read_state_page(query=_query(K.HOLDS))
    assert conn.executed == []


def test_fault_point_query_a_meta_row_is_required() -> None:
    conn = _Conn([])
    with pytest.raises(WorkLedgerReadError):
        _reader(conn).read_state_page(query=_query(K.HOLDS))
