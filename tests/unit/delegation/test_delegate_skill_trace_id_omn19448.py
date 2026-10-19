# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC2: the delegate-skill terminal's trace id reaches delegation_events.trace_id."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
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
    _project,
    _skill_terminal,
)

pytestmark = pytest.mark.unit
_TRACE_ID = "9c6a9b1e-3f7a-4b8e-8a5a-2c1d0e4f7a11"


@pytest.fixture(params=["inmemory", "sqlite"])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "sqlite":
        return SqliteDatabaseAdapter(tmp_path / "local.db")
    return InmemoryDatabaseAdapter()


def test_skill_terminal_stores_its_trace_id(db: Any) -> None:
    payload = _skill_terminal([])
    payload["trace_id"] = _TRACE_ID
    row = _project(payload, db)
    assert str(row["trace_id"]) == _TRACE_ID


def test_skill_terminal_without_trace_leaves_column_null(db: Any) -> None:
    row = _project(_skill_terminal([]), db)
    assert row.get("trace_id") is None


def test_sparse_skill_terminal_keeps_recorded_trace_id(db: Any) -> None:
    first = _skill_terminal([])
    first["trace_id"] = _TRACE_ID
    _project(first, db)
    later = _skill_terminal([])
    later["correlation_id"] = first["correlation_id"]
    row = _project(later, db)
    assert str(row["trace_id"]) == _TRACE_ID


async def test_async_runner_binds_skill_terminal_trace_id() -> None:
    runner = DelegationProjectionRunner()
    mock_db = _mock_db()
    mock_tenant_registry(mock_db)
    runner._db = mock_db
    payload = _skill_terminal([])
    payload["trace_id"] = _TRACE_ID
    cid = payload["correlation_id"]
    topic = runner._topic_delegate_skill_completed
    assert await runner.project_event(
        topic, payload, MessageMeta(partition=0, offset=0, fallback_id=cid)
    )
    (write,) = [
        call
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).startswith("INSERT INTO delegation_events")
    ]
    stored = _param_by_column(write.args)
    assert str(stored["trace_id"]) == _TRACE_ID

    async def read_stored(
        query: str, *args: object, **kwargs: object
    ) -> list[dict[str, object]]:
        return [stored] if query.startswith("SELECT * FROM delegation_events") else []

    mock_db.execute.reset_mock()
    mock_db.execute.side_effect = read_stored
    later = _skill_terminal([])
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
    assert str(preserved["trace_id"]) == _TRACE_ID
