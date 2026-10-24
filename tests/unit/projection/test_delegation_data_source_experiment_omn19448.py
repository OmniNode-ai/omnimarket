"""OMN-19448: classify terminal traffic from provenance through both writers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    mock_tenant_registry,
    seed_tenant_registry,
)
from tests.test_omn15905_delegation_projection_writer_seam import _param_by_column

ROOT = Path(__file__).resolve().parents[3]
CASES = [
    ("synthetic", "real", None, "experiment"),
    (None, "real", None, "real"),
    ("organic", "real", None, "real"),
    ("unclassified", "real", None, "real"),
    ("synthetic", "fixture", None, "fixture"),
    (
        None,
        "real",
        "fill-eval-run-capability-by-task-class-0f12eb5d0883-1-20261022T1433Z",
        "real",
    ),
]


def recorded_terminal(traffic_class: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        (
            ROOT
            / "tests/fixtures/delegation/recorded_delegate_skill_completed_local_accept.json"
        ).read_text()
    )
    payload["tenant_id"] = PROJECTION_TENANT_SLUG
    payload["correlation_id"] = str(uuid4())
    payload.pop("provenance", None)
    if traffic_class is not None:
        payload["provenance"] = {
            "source": "claude-code",
            "traffic_class": traffic_class,
            "source_surface": "eval-harness",
        }
    return payload


def recording_runner(
    publish: Any = None,
) -> tuple[DelegationProjectionRunner, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    db = AsyncMock()

    async def execute(query: str, *args: Any, **kwargs: Any) -> list[Any]:
        if query.lstrip().startswith("INSERT INTO delegation_events"):
            rows.append(_param_by_column((query, *args)))
        return []

    db.execute = AsyncMock(side_effect=execute)
    mock_tenant_registry(db)
    runner = DelegationProjectionRunner(publish_fn=publish)
    runner._db = db
    return runner, rows


@pytest.mark.parametrize(("traffic_class", "source", "lane", "expected"), CASES)
@pytest.mark.parametrize("store", ["inmemory", "sqlite"])
def test_sync_data_source(
    tmp_path: Path,
    store: str,
    traffic_class: str | None,
    source: str,
    lane: str | None,
    expected: str,
) -> None:
    db = (
        InmemoryDatabaseAdapter()
        if store == "inmemory"
        else SqliteDatabaseAdapter(tmp_path / "projection.db")
    )
    seed_tenant_registry(db)
    payload = recorded_terminal(traffic_class)
    payload["caller_lane"] = lane
    HandlerProjectionDelegation().handle({**payload, "data_source": source, "_db": db})
    (row,) = db.query(TABLE, {"correlation_id": payload["correlation_id"]})
    assert row["data_source"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("traffic_class", "source", "lane", "expected"), CASES)
async def test_async_data_source(
    traffic_class: str | None,
    source: str,
    lane: str | None,
    expected: str,
) -> None:
    runner, rows = recording_runner()
    payload = recorded_terminal(traffic_class)
    payload["caller_lane"] = lane
    payload["_envelope"] = {"metadata": {"tags": {"data_source": source}}}
    assert await runner.project_event(
        runner._topic_delegate_skill_completed,
        payload,
        MessageMeta(partition=0, offset=0, fallback_id=payload["correlation_id"]),
    )
    assert len(rows) == 1
    assert rows[0]["data_source"] == expected


def test_migration_and_rollback_keep_closed_checks() -> None:
    node = ROOT / "src/omnimarket/nodes/node_projection_delegation"
    name = "0064_delegation_events_data_source_experiment.sql"
    for directory, values in (
        ("migrations", "'real', 'fixture', 'experiment'"),
        ("rollback", "'real', 'fixture'"),
    ):
        sql = (node / directory / name).read_text()
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql
        assert "DROP CONSTRAINT IF EXISTS delegation_events_data_source_check" in sql
        assert f"CHECK (data_source IN ({values})) NOT VALID" in sql
    assert "fails if experiment rows exist" in (node / "rollback" / name).read_text()
