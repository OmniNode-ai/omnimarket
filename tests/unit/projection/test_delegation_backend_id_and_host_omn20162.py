# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20162: delegation_events names the backend and host of the accepting attempt.

AC1: a delegation terminal and its projection row carry ``backend_id`` and
``host`` of the accepting attempt (the first rung that passed its quality gate
with no failure class). AC2: a row written without them reads NULL, and a blank
value is stored as NULL, never as an empty backend.

``answering_backend`` (migration 0052) is the terminal's route, a different
datum, and stays what it was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
    _TERMINAL_OWNED_COLUMNS,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_delegation"
)


def _attempt(
    *,
    backend_id: str,
    host: str | None,
    passed: bool,
    failure_class: str | None = None,
) -> dict[str, Any]:
    return {
        "tier": "local" if passed else "cheap_cloud",
        "backend_id": backend_id,
        "model_id": "qwen3.8-27b",
        "quality_gate_passed": passed,
        "failure_class": failure_class,
        "host": host,
    }


def _skill_terminal(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "completed",
        "correlation_id": str(uuid4()),
        "task_type": "test",
        "provider": "local",
        "model_name": "qwen3.8-27b",
        "response": "ok",
        "quality_gate_passed": True,
        "quality_gates_failed": [],
        "attempts": attempts,
        "metrics": {
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "tokens_to_compliance": 30,
            "compliance_attempts": 1,
            "cost_usd": 0.0,
            "cost_savings_usd": 0.0,
            "latency_ms": 100,
        },
    }


def _canonical_terminal(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "_event_type": "delegation-completed",
        "correlation_id": str(uuid4()),
        "task_type": "test",
        "model_used": "qwen3.8-27b",
        "quality_passed": True,
        "route": "local-qwen",
    }
    payload.update(extra)
    return payload


def _project(payload: dict[str, Any], db: Any) -> dict[str, Any]:
    HandlerProjectionDelegation().handle({**payload, "_db": db})
    (row,) = db.query(TABLE, {"correlation_id": payload["correlation_id"]})
    return dict(row)


@pytest.mark.unit
def test_backend_id_and_host_of_the_accepting_attempt_reach_the_row() -> None:
    payload = _skill_terminal(
        [
            _attempt(
                backend_id="local-a",
                host="h201",
                passed=False,
                failure_class="model_unavailable",
            ),
            _attempt(backend_id="local-b", host="h202", passed=True),
        ]
    )
    row = _project(payload, InmemoryDatabaseAdapter())
    assert row["backend_id"] == "local-b"
    assert row["host"] == "h202"


@pytest.mark.unit
def test_backend_id_and_host_reach_the_sqlite_local_store(tmp_path: Path) -> None:
    payload = _skill_terminal(
        [_attempt(backend_id="local-b", host="h202", passed=True)]
    )
    row = _project(payload, SqliteDatabaseAdapter(tmp_path / "local.db"))
    assert row["backend_id"] == "local-b"
    assert row["host"] == "h202"


@pytest.mark.unit
def test_backend_id_and_host_ride_the_canonical_terminal() -> None:
    payload = _canonical_terminal(backend_id="local-b", host="h202")
    row = _project(payload, InmemoryDatabaseAdapter())
    assert row["backend_id"] == "local-b"
    assert row["host"] == "h202"
    # The route stays its own datum.
    assert row["answering_backend"] == "local-qwen"


@pytest.mark.unit
def test_backend_id_and_host_do_not_repurpose_answering_backend() -> None:
    payload = _canonical_terminal(backend_id="local-b", host="h202", route="other")
    row = _project(payload, InmemoryDatabaseAdapter())
    assert row["answering_backend"] == "other"
    assert row["backend_id"] == "local-b"


@pytest.mark.unit
def test_a_refused_ladder_names_no_backend_id_or_host() -> None:
    payload = _skill_terminal(
        [_attempt(backend_id="local-a", host="h201", passed=False)]
    )
    row = _project(payload, InmemoryDatabaseAdapter())
    assert row.get("backend_id") is None
    assert row.get("host") is None


@pytest.mark.unit
def test_null_backend_when_the_terminal_names_none() -> None:
    row = _project(_canonical_terminal(), InmemoryDatabaseAdapter())
    assert row.get("backend_id") is None
    assert row.get("host") is None


@pytest.mark.unit
@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_null_backend_when_the_value_is_blank(blank: str) -> None:
    canonical = _project(
        _canonical_terminal(backend_id=blank, host=blank), InmemoryDatabaseAdapter()
    )
    assert canonical.get("backend_id") is None
    assert canonical.get("host") is None
    skill = _project(
        # The attempt model refuses an empty backend_id outright, so a
        # whitespace-only one is the blank it can carry.
        _skill_terminal([_attempt(backend_id=blank or " ", host=blank, passed=True)]),
        InmemoryDatabaseAdapter(),
    )
    assert skill.get("backend_id") is None
    assert skill.get("host") is None


@pytest.mark.unit
def test_null_backend_for_a_row_written_before_the_columns(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "local.db")
    legacy = _canonical_terminal()
    legacy_row = _project(legacy, db)
    assert legacy_row.get("backend_id") is None
    assert legacy_row.get("host") is None
    # A later row that does carry the columns widens the table; the legacy row
    # still reads NULL, not an empty string.
    _project(_canonical_terminal(backend_id="local-b", host="h202"), db)
    (reread,) = db.query(TABLE, {"correlation_id": legacy["correlation_id"]})
    assert reread["backend_id"] is None
    assert reread["host"] is None


@pytest.mark.unit
def test_a_later_sparse_terminal_keeps_the_recorded_backend_id_and_host() -> None:
    db = InmemoryDatabaseAdapter()
    first = _canonical_terminal(backend_id="local-b", host="h202")
    _project(first, db)
    later = _canonical_terminal()
    later["correlation_id"] = first["correlation_id"]
    row = _project(later, db)
    assert row["backend_id"] == "local-b"
    assert row["host"] == "h202"


@pytest.mark.unit
def test_the_terminal_owns_backend_id_and_host_as_one_unit() -> None:
    assert "backend_id" in _TERMINAL_OWNED_COLUMNS
    assert "host" in _TERMINAL_OWNED_COLUMNS


@pytest.mark.unit
def test_the_contract_lists_the_columns_and_the_migration_adds_them() -> None:
    contract = (_NODE / "contract.yaml").read_text()
    sql = (
        _NODE / "migrations/0054_delegation_events_backend_id_and_host.sql"
    ).read_text()
    for column in ("backend_id", "host"):
        assert f"- {column}\n" in contract
        assert f"ADD COLUMN IF NOT EXISTS {column} TEXT" in sql
