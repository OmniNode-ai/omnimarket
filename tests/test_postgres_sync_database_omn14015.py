# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14015: PostgresSyncProjectionAdapter — the sync Postgres projection boundary.

The adapter is the sync counterpart of ``AsyncpgAdapter`` that lets the bus-less
``onex delegate`` CLI path project its evidence onto the platform Postgres
substrate (by overlay). These tests prove the generated SQL WITHOUT a live
Postgres, by driving a fake psycopg2 connection that captures every executed
statement + params:

* UPSERT builds ``INSERT ... ON CONFLICT (<keys>) DO UPDATE SET ...`` keyed on the
  conflict column(s);
* list/dict values are adapted to JSONB via ``psycopg2.extras.Json`` (the
  ``*_jsonb`` / ``premium_counterfactual`` columns are JSONB), while scalars pass
  through;
* the adapter NEVER mutates the schema (no CREATE/ALTER) — migrations own the
  table;
* table/column identifiers are validated (injection-safe);
* query builds a parameterized ``SELECT ... WHERE`` and returns dict rows.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg2  # type: ignore[import-untyped]
import psycopg2.extras  # type: ignore[import-untyped]
import pytest

from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter

_DSN = "postgresql://role:secret@dev-postgres:5432/omnidash_analytics"


class _FakeCursor:
    """Captures executed SQL/params; returns preset rows for SELECT."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: str, params: Any = None) -> None:
        self.executed.append((statement, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.autocommit = False
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self.cursors: list[_FakeCursor] = []
        self._rows = rows or []

    def cursor(self, cursor_factory: Any = None) -> _FakeCursor:
        cur = _FakeCursor(self._rows)
        self.cursors.append(cur)
        return cur

    def close(self) -> None:
        self.closed = True

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def fake_conn(monkeypatch: pytest.MonkeyPatch) -> _FakeConn:
    conn = _FakeConn()
    monkeypatch.setattr(psycopg2, "connect", lambda _dsn: conn)
    return conn


def test_upsert_builds_on_conflict_do_update(fake_conn: _FakeConn) -> None:
    adapter = PostgresSyncProjectionAdapter(_DSN)
    row: dict[str, object] = {
        "correlation_id": "abc-123",
        "task_type": "code_generation",
        "quality_gate_passed": True,
        "cost_usd": Decimal("0.0012"),
    }

    ok = adapter.upsert("delegation_events", "correlation_id", row)

    assert ok is True
    assert fake_conn.autocommit is True
    assert fake_conn.closed is True
    assert fake_conn.commits == 1
    assert fake_conn.rollbacks == 0
    statement, params = fake_conn.cursors[-1].executed[0]
    assert statement.startswith("INSERT INTO delegation_events (")
    assert "ON CONFLICT (correlation_id) DO UPDATE SET" in statement
    # The conflict key is excluded from the SET clause; the other columns update.
    assert "task_type = EXCLUDED.task_type" in statement
    assert "cost_usd = EXCLUDED.cost_usd" in statement
    assert "correlation_id = EXCLUDED.correlation_id" not in statement
    # Scalars (incl. Decimal/bool) bind natively — never wrapped in Json.
    assert params["cost_usd"] == Decimal("0.0012")
    assert params["quality_gate_passed"] is True


def test_upsert_adapts_list_and_dict_to_jsonb(fake_conn: _FakeConn) -> None:
    adapter = PostgresSyncProjectionAdapter(_DSN)
    row: dict[str, object] = {
        "correlation_id": "abc-123",
        "quality_gates_failed_jsonb": ["refusal", "empty"],
        "premium_counterfactual": {"model": "cloud-premium", "cost_usd": 0.9},
    }

    adapter.upsert("delegation_events", "correlation_id", row)

    _, params = fake_conn.cursors[-1].executed[0]
    assert isinstance(params["quality_gates_failed_jsonb"], psycopg2.extras.Json)
    assert isinstance(params["premium_counterfactual"], psycopg2.extras.Json)
    # Scalar conflict key is not JSON-wrapped.
    assert params["correlation_id"] == "abc-123"


def test_upsert_do_nothing_when_only_conflict_key(fake_conn: _FakeConn) -> None:
    adapter = PostgresSyncProjectionAdapter(_DSN)

    adapter.upsert("delegation_events", "correlation_id", {"correlation_id": "x"})

    statement, _ = fake_conn.cursors[-1].executed[0]
    assert "ON CONFLICT (correlation_id) DO NOTHING" in statement


def test_upsert_never_mutates_schema(fake_conn: _FakeConn) -> None:
    adapter = PostgresSyncProjectionAdapter(_DSN)

    adapter.upsert(
        "delegation_events",
        "correlation_id",
        {"correlation_id": "x", "task_type": "test"},
    )

    for statement, _ in fake_conn.cursors[-1].executed:
        upper = statement.upper()
        assert "ALTER TABLE" not in upper
        assert "CREATE TABLE" not in upper


def test_upsert_missing_conflict_key_raises(fake_conn: _FakeConn) -> None:
    adapter = PostgresSyncProjectionAdapter(_DSN)
    with pytest.raises(KeyError):
        adapter.upsert("delegation_events", "correlation_id", {"task_type": "test"})


def test_invalid_identifier_raises_before_any_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_dsn: str) -> Any:
        raise AssertionError("must validate identifiers before connecting")

    monkeypatch.setattr(psycopg2, "connect", _boom)
    adapter = PostgresSyncProjectionAdapter(_DSN)

    with pytest.raises(ValueError, match="invalid table identifier"):
        adapter.upsert(
            "delegation_events; DROP TABLE x",
            "correlation_id",
            {"correlation_id": "x"},
        )


def test_query_builds_parameterized_select(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"correlation_id": "abc-123", "task_type": "test"}]
    conn = _FakeConn(rows=rows)
    monkeypatch.setattr(psycopg2, "connect", lambda _dsn: conn)
    adapter = PostgresSyncProjectionAdapter(_DSN)

    result = adapter.query("delegation_events", {"correlation_id": "abc-123"})

    assert result == rows
    statement, params = conn.cursors[-1].executed[0]
    assert (
        statement
        == "SELECT * FROM delegation_events WHERE correlation_id = %(correlation_id)s"
    )
    assert params == {"correlation_id": "abc-123"}
    assert conn.closed is True


def test_query_without_filters_selects_all(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn(rows=[])
    monkeypatch.setattr(psycopg2, "connect", lambda _dsn: conn)
    adapter = PostgresSyncProjectionAdapter(_DSN)

    adapter.query("delegation_events")

    statement, _ = conn.cursors[-1].executed[0]
    assert statement == "SELECT * FROM delegation_events"


def test_empty_dsn_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty DSN"):
        PostgresSyncProjectionAdapter("   ")
