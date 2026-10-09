# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC2: delegation_events stores the terminal's trace and routing columns.

The columns below each have a producer field on the canonical terminal wire
model (``omnibase_core`` ``ModelDelegationResult``):

* ``trace_id``           <- ``trace_id`` (OMN-19437)
* ``routed_model``       <- ``model_used``
* ``answering_backend``  <- ``route``

``requested_model``, ``queue_wait_ms`` and ``execution_ms`` now have producer
fields: the skill terminal carries ``attempts[0].model_id``, ``queue_wait_ms``
and ``execution_duration_ms``. Canonical payloads may explicitly name them.

The falsifier: apply the migration, project a terminal, assert each column.
The SQL file is checked statically here; the real-Postgres twin below applies
it to a disposable schema when a server is reachable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    seed_tenant_registry,
)

_MIGRATIONS = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_delegation/migrations"
)
_NEW_COLUMNS = ("trace_id", "routed_model", "answering_backend")
_TRACE_ID = "6f1d2e0b-9a57-4c1e-8f0e-3e1f9c0a4d99"


def _terminal(
    *,
    event_type: str = "delegation-failed",
    trace_id: str | None = _TRACE_ID,
    route: str | None = "local-qwen",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tenant_id": PROJECTION_TENANT_SLUG,
        "_event_type": event_type,
        "correlation_id": str(uuid4()),
        "task_type": "test",
        "model_used": "qwen3-coder-30b",
        "quality_passed": False,
        "failure_reason": "HTTP 429: rate limit exceeded",
        "operational_outcome": "provider_quota",
        "content_verdict": "not_applicable",
        "terminal_failure_cause": "provider_quota_exhausted",
    }
    if trace_id is not None:
        payload["trace_id"] = trace_id
    if route is not None:
        payload["route"] = route
    return payload


@pytest.mark.unit
def test_a_migration_adds_each_new_column_idempotently() -> None:
    candidates = [
        path
        for path in sorted(_MIGRATIONS.glob("00*_delegation_events_*.sql"))
        if all(column in path.read_text() for column in _NEW_COLUMNS)
    ]
    assert len(candidates) == 1, "exactly one migration owns the AC2 columns"
    sql = candidates[0].read_text()
    for column in _NEW_COLUMNS:
        assert re.search(rf"ADD COLUMN IF NOT EXISTS {column}\s+(TEXT|UUID)", sql), (
            f"{column} must be added IF NOT EXISTS"
        )
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql


@pytest.mark.unit
def test_only_migration_0058_adds_requested_model_and_timing() -> None:
    name = "0058_delegation_events_requested_model_and_timing.sql"
    for column, sql_type in (
        ("requested_model", "TEXT"),
        ("queue_wait_ms", "INTEGER"),
        ("execution_ms", "INTEGER"),
    ):
        owners = [
            path
            for path in _MIGRATIONS.glob("*.sql")
            if re.search(
                rf"ADD\s+COLUMN\s+(?:IF NOT EXISTS\s+)?{column}\b", path.read_text()
            )
        ]
        assert [path.name for path in owners] == [name], (
            f"only migration 0058 may add {column}"
        )
        sql = owners[0].read_text()
        assert re.search(rf"ADD COLUMN IF NOT EXISTS {column}\s+{sql_type}\b", sql)
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql
    assert (_MIGRATIONS.parent / "rollback" / name).is_file()


@pytest.mark.unit
@pytest.mark.parametrize("event_type", ["delegation-failed", "delegation-completed"])
def test_inmemory_row_carries_trace_model_and_backend(event_type: str) -> None:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    payload = _terminal(event_type=event_type)
    correlation_id = payload["correlation_id"]
    HandlerProjectionDelegation().handle({**payload, "_db": db})
    (row,) = db.query(TABLE, {"correlation_id": correlation_id})
    assert row["trace_id"] == _TRACE_ID
    assert row["routed_model"] == "qwen3-coder-30b"
    assert row["answering_backend"] == "local-qwen"
    # The AC1 and K1 columns ride the same row, copied from the terminal.
    assert row["terminal_failure_cause"] == "provider_quota_exhausted"
    assert row["operational_outcome"] == "provider_quota"
    assert row["content_verdict"] == "not_applicable"


@pytest.mark.unit
def test_sqlite_local_store_row_carries_the_same_columns(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "local.db")
    seed_tenant_registry(db)
    payload = _terminal()
    correlation_id = payload["correlation_id"]
    HandlerProjectionDelegation().handle({**payload, "_db": db})
    (row,) = db.query(TABLE, {"correlation_id": correlation_id})
    assert row["trace_id"] == _TRACE_ID
    assert row["routed_model"] == "qwen3-coder-30b"
    assert row["answering_backend"] == "local-qwen"
    assert row["terminal_failure_cause"] == "provider_quota_exhausted"
    assert row["operational_outcome"] == "provider_quota"
    assert row["content_verdict"] == "not_applicable"


@pytest.mark.unit
def test_a_terminal_without_trace_or_route_leaves_them_null() -> None:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    payload = _terminal(trace_id=None, route=None)
    HandlerProjectionDelegation().handle({**payload, "_db": db})
    (row,) = db.query(TABLE, {"correlation_id": payload["correlation_id"]})
    assert row.get("trace_id") is None
    assert row.get("answering_backend") is None
    assert row["routed_model"] == "qwen3-coder-30b"


@pytest.mark.unit
def test_a_later_terminal_without_trace_does_not_erase_the_recorded_one() -> None:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    first = _terminal()
    HandlerProjectionDelegation().handle({**first, "_db": db})
    later = _terminal(trace_id=None, route=None)
    later["correlation_id"] = first["correlation_id"]
    HandlerProjectionDelegation().handle({**later, "_db": db})
    (row,) = db.query(TABLE, {"correlation_id": first["correlation_id"]})
    assert row["trace_id"] == _TRACE_ID
    assert row["answering_backend"] == "local-qwen"
