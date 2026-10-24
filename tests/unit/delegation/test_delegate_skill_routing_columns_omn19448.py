# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC109: delegate-skill terminals store their accepting attempt's routing."""

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


def _ladder() -> list[dict[str, Any]]:
    return [
        {
            "tier": "cheap_cloud",
            "backend_id": "openrouter-x",
            "model_id": "m-refused",
            "quality_gate_passed": False,
            "failure_class": "model_unavailable",
            "host": None,
        },
        {
            "tier": "local",
            "backend_id": "local-coder",
            "model_id": "Qwen3.8-27B",
            "quality_gate_passed": True,
            "failure_class": None,
            "host": "h201",
        },
    ]


def test_accepting_attempt_stores_routed_model_and_answering_backend(db: Any) -> None:
    row = _project(_skill_terminal(_ladder()), db)
    assert row["routed_model"] == "Qwen3.8-27B"
    assert row["answering_backend"] == "local-coder"
    assert row["routed_model"] != "m-refused"
    assert row["answering_backend"] != "openrouter-x"


def test_refused_ladder_names_its_rung_but_not_an_answering_backend(db: Any) -> None:
    """OMN-19448 rule 2: a failed run names the model it was refused on.

    The refused rung did not deliver an answer, so ``answering_backend`` stays
    NULL.
    """
    row = _project(_skill_terminal(_ladder()[:1]), db)
    assert row["routed_model"] == "m-refused"
    assert row.get("answering_backend") is None


def test_blank_backend_leaves_answering_backend_null(db: Any) -> None:
    attempts = _ladder()
    attempts[1]["backend_id"] = " "
    row = _project(_skill_terminal(attempts), db)
    assert row["routed_model"] == "Qwen3.8-27B"
    assert row.get("answering_backend") is None


def test_sparse_skill_terminal_keeps_recorded_routing_columns(db: Any) -> None:
    first = _skill_terminal(_ladder())
    _project(first, db)
    later = _skill_terminal([])
    later["correlation_id"] = first["correlation_id"]
    row = _project(later, db)
    assert row["routed_model"] == "Qwen3.8-27B"
    assert row["answering_backend"] == "local-coder"


def test_skill_terminal_agrees_with_canonical_routing_columns(db: Any) -> None:
    first = _canonical_terminal(model_used="Qwen3.8-27B", route="local-coder")
    _project(first, db)
    later = _skill_terminal(_ladder())
    later["correlation_id"] = first["correlation_id"]
    row = _project(later, db)
    assert row["routed_model"] == "Qwen3.8-27B"
    assert row["answering_backend"] == "local-coder"


async def test_async_runner_binds_and_preserves_skill_terminal_routing_columns() -> (
    None
):
    runner = DelegationProjectionRunner()
    mock_db = _mock_db()
    mock_tenant_registry(mock_db)
    runner._db = mock_db
    payload = _skill_terminal(_ladder())
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
    assert stored["routed_model"] == "Qwen3.8-27B"
    assert stored["answering_backend"] == "local-coder"

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
    assert preserved["routed_model"] == "Qwen3.8-27B"
    assert preserved["answering_backend"] == "local-coder"
