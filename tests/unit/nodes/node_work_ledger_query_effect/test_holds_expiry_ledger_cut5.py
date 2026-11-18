# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A HOLD past its until_at leaves the in-force answers (OMN-24555). Every
statement reads the is_open column of the contract-declared state relation and
binds its values; a database error is a read error."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg2 import sql as pgsql

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerStateQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    STATE_PAGE_COLUMNS,
    ModelWorkLedgerQuerySource,
    PostgresWorkLedgerQueryReader,
    WorkLedgerReadError,
)

pytestmark = pytest.mark.unit

K = EnumWorkLedgerQueryKind
NOW = datetime(2026, 11, 2, 3, tzinfo=UTC)
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


def test_holds_statement_keeps_a_past_hold_out_whatever_its_surface() -> None:
    conn = _Conn([_meta_only()])
    _reader(conn).read_state_page(query=_query(K.HOLDS))
    sql, params = conn.executed[0]
    assert "s.until_at IS NULL OR s.until_at >=" in sql
    assert "scope_surface IS NOT NULL AND s.until_at" not in sql
    assert NOW in params


def test_holds_expired_count_filter_does_not_mention_scope_surface() -> None:
    conn = _Conn([_meta_only()])
    _reader(conn).read_state_page(query=_query(K.HOLDS))
    sql, _ = conn.executed[0]
    assert "count(*) FILTER (WHERE kind = 'hold' AND is_open AND until_at <" in sql
    assert "scope_surface IS NOT NULL" not in sql


def test_inbox_statement_drops_expired_holds_and_binds_now_before_the_lanes() -> None:
    conn = _Conn([_meta_only()])
    _reader(conn).read_state_page(query=_query(K.INBOX, lanes=("lane-x",)))
    sql, params = conn.executed[0]
    assert "s.kind <> 'hold' OR s.until_at IS NULL OR s.until_at >=" in sql
    # Placeholder order: the meta kinds, the meta now, the page kinds, the
    # expiry now, then the lanes.
    assert list(params) == [
        ["msg", "hold"],
        NOW,
        ["msg", "hold"],
        NOW,
        ["lane-x", "all"],
        11,
    ]
    assert params.index(NOW) < params.index(["lane-x", "all"])


def test_fault_holds_connection_error_is_a_read_error_and_closes() -> None:
    conn = _Conn([_meta_only()], fault=RuntimeError("boom"))
    with pytest.raises(WorkLedgerReadError, match="RuntimeError"):
        _reader(conn).read_state_page(query=_query(K.HOLDS))
    assert conn.closed


@pytest.mark.parametrize("point", [K.HOLDS, K.INBOX])
def test_placeholder_count_matches_the_bound_params(
    point: EnumWorkLedgerQueryKind,
) -> None:
    conn = _Conn([_meta_only()])
    _reader(conn).read_state_page(query=_query(point, lanes=("lane-x",)))
    sql, params = conn.executed[0]
    assert sql.count("%s") == len(params)
