# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC2: terminal requested model and measured timings reach the row.

The first requested model can differ from the answering model. Missing timings
mean NULL; a measured zero remains zero, including across sparse terminals.
"""

from __future__ import annotations

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
_COLUMNS = ("requested_model", "queue_wait_ms", "execution_ms")


@pytest.fixture(params=["inmemory", "sqlite"])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "sqlite":
        return SqliteDatabaseAdapter(tmp_path / "local.db")
    return InmemoryDatabaseAdapter()


def _timed_skill_terminal() -> dict[str, Any]:
    first = _attempt(backend_id="first", host="h201", passed=False)
    first["model_id"] = "first-requested-model"
    payload = _skill_terminal(
        [first, _attempt(backend_id="answering", host="h202", passed=True)]
    )
    payload.update(queue_wait_ms=1234, execution_duration_ms=567)
    return payload


def test_skill_terminal_stores_first_requested_model_and_timings(db: Any) -> None:
    payload = _timed_skill_terminal()
    row = _project(payload, db)
    assert row["requested_model"] == "first-requested-model"
    assert row["model_name"] == payload["model_name"] == "qwen3.8-27b"
    assert row["requested_model"] != row["model_name"]
    assert row["queue_wait_ms"] == 1234
    assert row["execution_ms"] == 567


@pytest.mark.parametrize("path", ["skill", "canonical"])
def test_measured_zero_is_stored_and_can_replace_a_nonzero_timing(
    db: Any, path: str
) -> None:
    payload = (
        _timed_skill_terminal()
        if path == "skill"
        else _canonical_terminal(queue_wait_ms=1234, execution_duration_ms=567)
    )
    _project(payload, db)
    payload.update(queue_wait_ms=0, execution_duration_ms=0)
    row = _project(payload, db)
    assert row["queue_wait_ms"] == 0
    assert row["execution_ms"] == 0


@pytest.mark.parametrize("path", ["skill", "canonical"])
def test_unmeasured_terminal_leaves_all_three_null(db: Any, path: str) -> None:
    payload = _skill_terminal([]) if path == "skill" else _canonical_terminal()
    payload.update(queue_wait_ms=None, execution_duration_ms=None)
    row = _project(payload, db)
    for column in _COLUMNS:
        assert row.get(column) is None


@pytest.mark.parametrize("path", ["skill", "canonical"])
@pytest.mark.parametrize("queue_wait_ms", [1234, 0])
def test_sparse_terminal_keeps_recorded_model_and_timings(
    db: Any, path: str, queue_wait_ms: int
) -> None:
    first = (
        _timed_skill_terminal()
        if path == "skill"
        else _canonical_terminal(
            requested_model="first-requested-model", execution_duration_ms=567
        )
    )
    first["queue_wait_ms"] = queue_wait_ms
    _project(first, db)
    later = _skill_terminal([]) if path == "skill" else _canonical_terminal()
    later["correlation_id"] = first["correlation_id"]
    row = _project(later, db)
    assert row["requested_model"] == "first-requested-model"
    assert row["queue_wait_ms"] == queue_wait_ms
    assert row["execution_ms"] == 567


def test_canonical_terminal_carries_explicit_fields(db: Any) -> None:
    payload = _canonical_terminal(
        requested_model="first-requested-model",
        queue_wait_ms=1234,
        execution_duration_ms=567,
        escalation_history=[{"model_used": "other-model"}],
    )
    row = _project(payload, db)
    assert row["requested_model"] == "first-requested-model"
    assert row["routed_model"] == payload["model_used"]
    assert row["requested_model"] != row["routed_model"]
    assert row["queue_wait_ms"] == 1234
    assert row["execution_ms"] == 567


@pytest.mark.parametrize("invalid", [True, False, -1, 1.5, "1234", ""])
def test_canonical_timings_reject_non_integer_or_negative_values(
    db: Any, invalid: object
) -> None:
    row = _project(
        _canonical_terminal(queue_wait_ms=invalid, execution_duration_ms=invalid), db
    )
    assert row.get("queue_wait_ms") is None
    assert row.get("execution_ms") is None


@pytest.mark.parametrize("blank", ["", "  ", "\t"])
def test_blank_requested_model_is_not_minted_from_a_later_attempt(
    db: Any, blank: str
) -> None:
    payload = _timed_skill_terminal()
    payload["attempts"][0]["model_id"] = blank
    assert _project(payload, db).get("requested_model") is None
    canonical = _canonical_terminal(
        requested_model=blank, escalation_history=[{"model_used": "first-model"}]
    )
    assert _project(canonical, db).get("requested_model") is None


def test_canonical_does_not_invent_requested_model_from_model_used(db: Any) -> None:
    row = _project(
        _canonical_terminal(escalation_history=[{"model_used": "first-model"}]), db
    )
    assert row.get("requested_model") is None


def test_sqlite_declares_nullable_columns_before_any_terminal(tmp_path: Path) -> None:
    path = tmp_path / "local.db"
    _project(_skill_terminal([]), SqliteDatabaseAdapter(path))
    with sqlite3.connect(path) as conn:
        columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(delegation_events)")
        }
    for column, sql_type in zip(_COLUMNS, ("TEXT", "INTEGER", "INTEGER"), strict=True):
        assert columns[column][2] == sql_type
        assert columns[column][3] == 0  # nullable
        assert columns[column][4] is None  # no default


def test_contract_lists_columns_and_terminal_owns_them() -> None:
    contract = (_NODE / "contract.yaml").read_text()
    for column in _COLUMNS:
        assert f"- {column}\n" in contract
        assert column in _TERMINAL_OWNED_COLUMNS


@pytest.mark.parametrize("path", ["skill", "canonical"])
@pytest.mark.parametrize("queue_wait_ms", [1234, 0])
async def test_async_runner_binds_the_same_columns_and_preserves_sparse_evidence(
    path: str, queue_wait_ms: int
) -> None:
    runner = DelegationProjectionRunner()
    mock_db = _mock_db()
    mock_tenant_registry(mock_db)
    runner._db = mock_db
    payload = (
        _timed_skill_terminal()
        if path == "skill"
        else _canonical_terminal(
            requested_model="first-requested-model", execution_duration_ms=567
        )
    )
    payload["queue_wait_ms"] = queue_wait_ms
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
    assert stored["requested_model"] == "first-requested-model"
    assert stored["queue_wait_ms"] == queue_wait_ms
    assert stored["execution_ms"] == 567

    async def read_stored(
        query: str, *args: object, **kwargs: object
    ) -> list[dict[str, object]]:
        return [stored] if query.startswith("SELECT * FROM delegation_events") else []

    mock_db.execute.reset_mock()
    mock_db.execute.side_effect = read_stored
    later = _skill_terminal([]) if path == "skill" else _canonical_terminal()
    later["correlation_id"] = cid
    assert await runner.project_event(
        topic, later, MessageMeta(partition=0, offset=1, fallback_id=cid)
    )
    (write,) = [
        call
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).startswith("INSERT INTO delegation_events")
    ]
    preserved = _param_by_column(write.args)
    for column in _COLUMNS:
        assert preserved[column] == stored[column]
