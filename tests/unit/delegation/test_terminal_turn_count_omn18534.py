# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18534: the terminal's turn count is a typed delegation_events column.

``turn_count`` is the terminal's ``attempts_count``, the authoritative total of
inference calls. It is written from the terminal, never derived at read time,
on the delegate-skill terminal and the canonical terminal alike. A delegation
that failed before any turn stores 0, a reported 0 survives a sparse later
terminal, and a terminal that reported nothing leaves NULL.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
    _TERMINAL_OWNED_COLUMNS,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.helpers.tenant_registry import mock_tenant_registry
from tests.test_omn15905_delegation_projection_writer_seam import (
    _mock_db,
    _param_by_column,
)
from tests.unit.projection.test_delegation_backend_id_and_host_omn20162 import (
    _NODE,
    _attempt,
    _canonical_terminal,
    _project,
    _skill_terminal,
)

pytestmark = pytest.mark.unit


@pytest.fixture(params=["inmemory", "sqlite"])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "sqlite":
        return SqliteDatabaseAdapter(tmp_path / "local.db")
    return InmemoryDatabaseAdapter()


def _single_turn_skill_terminal() -> dict[str, Any]:
    return _skill_terminal([_attempt(backend_id="local", host="h201", passed=True)])


def _escalated_skill_terminal() -> dict[str, Any]:
    """Three inference calls, one tier escalation: the two counts differ."""
    payload = _skill_terminal(
        [
            _attempt(
                backend_id="local-a",
                host="h201",
                passed=False,
                failure_class="quality_gate",
            ),
            _attempt(
                backend_id="local-a",
                host="h201",
                passed=False,
                failure_class="quality_gate",
            ),
            _attempt(backend_id="cloud", host=None, passed=True),
        ]
    )
    payload["escalation_count"] = 1
    return payload


def _refused_before_dispatch_skill_terminal() -> dict[str, Any]:
    payload = _skill_terminal([])
    payload.update(
        status="failed",
        quality_gate_passed=False,
        terminal_failure_cause="refused_before_dispatch",
        attempts_count=0,
        error_message="refused before dispatch",
    )
    return payload


def test_single_turn_and_escalated_skill_terminals_store_different_counts(
    db: Any,
) -> None:
    single = _project(_single_turn_skill_terminal(), db)
    escalated = _project(_escalated_skill_terminal(), db)
    assert single["turn_count"] == 1
    assert escalated["turn_count"] == 3
    assert single["turn_count"] != escalated["turn_count"]


def test_escalated_terminal_reports_total_calls_not_the_pre_escalation_count(
    db: Any,
) -> None:
    row = _project(_escalated_skill_terminal(), db)
    assert row["escalation_count"] == 1
    assert row["turn_count"] == 3
    assert row["turn_count"] != row["escalation_count"]


def test_failure_before_any_turn_stores_zero_not_null(db: Any) -> None:
    row = _project(_refused_before_dispatch_skill_terminal(), db)
    assert "turn_count" in row
    assert row["turn_count"] is not None
    assert row["turn_count"] == 0


@pytest.mark.parametrize("attempts_count", [0, 1, 4])
def test_canonical_terminal_stores_the_reported_count(
    db: Any, attempts_count: int
) -> None:
    row = _project(_canonical_terminal(attempts_count=attempts_count), db)
    assert row["turn_count"] == attempts_count


def test_canonical_escalated_terminal_stores_total_calls(db: Any) -> None:
    row = _project(
        _canonical_terminal(
            attempts_count=4,
            escalation_count=1,
            escalation_history=[{"model_used": "a"}, {"model_used": "b"}],
        ),
        db,
    )
    assert row["escalation_count"] == 1
    assert row["turn_count"] == 4


def test_terminal_that_reported_no_count_leaves_null(db: Any) -> None:
    row = _project(_canonical_terminal(), db)
    assert row.get("turn_count") is None


@pytest.mark.parametrize("invalid", [True, False, -1, 1.5, "2", ""])
def test_canonical_count_rejects_non_integer_or_negative_values(
    db: Any, invalid: object
) -> None:
    row = _project(_canonical_terminal(attempts_count=invalid), db)
    assert row.get("turn_count") is None


@pytest.mark.parametrize("path", ["skill", "canonical"])
@pytest.mark.parametrize("recorded", [3, 0])
def test_sparse_later_terminal_keeps_the_recorded_count(
    db: Any, path: str, recorded: int
) -> None:
    if path == "skill":
        first = (
            _escalated_skill_terminal()
            if recorded
            else _refused_before_dispatch_skill_terminal()
        )
        later = _canonical_terminal()
    else:
        first = _canonical_terminal(attempts_count=recorded)
        later = _canonical_terminal()
    _project(first, db)
    later["correlation_id"] = first["correlation_id"]
    assert _project(later, db)["turn_count"] == recorded


def test_sqlite_declares_a_nullable_integer_column_before_any_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local.db"
    _project(_single_turn_skill_terminal(), SqliteDatabaseAdapter(path))
    with sqlite3.connect(path) as conn:
        columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(delegation_events)")
        }
    assert columns["turn_count"][2] == "INTEGER"
    assert columns["turn_count"][3] == 0  # nullable
    assert columns["turn_count"][4] is None  # no default


def test_projection_contract_exposes_the_column_and_terminal_owns_it() -> None:
    contract = (_NODE / "contract.yaml").read_text()
    assert "- turn_count\n" in contract
    assert "turn_count" in _TERMINAL_OWNED_COLUMNS


def test_only_migration_0068_adds_turn_count_and_ships_a_rollback() -> None:
    migrations = _NODE / "migrations"
    adders = [
        path.name
        for path in sorted(migrations.glob("*.sql"))
        if re.search(r"ADD COLUMN[^;]*\bturn_count\b", path.read_text())
    ]
    assert adders == ["0068_delegation_events_turn_count.sql"]
    sql = (migrations / adders[0]).read_text()
    assert "ADD COLUMN IF NOT EXISTS turn_count INTEGER;" in sql
    assert "NOT NULL" not in sql
    assert "DEFAULT" not in sql.upper().replace("-- ", "").split("BEGIN;")[1]
    rollback = (_NODE / "rollback" / adders[0]).read_text()
    assert "DROP COLUMN IF EXISTS turn_count" in rollback


@pytest.mark.parametrize("path", ["skill", "canonical"])
async def test_async_runner_binds_the_column_and_preserves_a_reported_zero(
    path: str,
) -> None:
    runner = DelegationProjectionRunner()
    mock_db = _mock_db()
    mock_tenant_registry(mock_db)
    runner._db = mock_db
    payload = (
        _refused_before_dispatch_skill_terminal()
        if path == "skill"
        else _canonical_terminal(attempts_count=0)
    )
    cid = payload["correlation_id"]
    topic = (
        runner._topic_delegate_skill_completed
        if path == "skill"
        else runner._topic_delegation_completed
    )
    assert await runner.project_event(
        topic, payload, MessageMeta(partition=0, offset=0, fallback_id=cid)
    )
    (write,) = [
        call
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).startswith("INSERT INTO delegation_events")
    ]
    stored = _param_by_column(write.args)
    assert stored["turn_count"] == 0

    async def read_stored(
        query: str, *args: object, **kwargs: object
    ) -> list[dict[str, object]]:
        return [stored] if query.startswith("SELECT * FROM delegation_events") else []

    mock_db.execute.reset_mock()
    mock_db.execute.side_effect = read_stored
    later = _canonical_terminal()
    later["correlation_id"] = cid
    assert await runner.project_event(
        runner._topic_delegation_completed,
        later,
        MessageMeta(partition=0, offset=1, fallback_id=cid),
    )
    (write,) = [
        call
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).startswith("INSERT INTO delegation_events")
    ]
    assert _param_by_column(write.args)["turn_count"] == 0
