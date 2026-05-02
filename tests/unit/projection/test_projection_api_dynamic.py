"""Unit tests for the dynamic projection API (OMN-10490).

Tests that the projection API server honours the contract-driven topic map:
- Response columns match contract declaration
- DEGRADED entries return 503 with reason
- Unknown topic returns 404 with available topic list
- GET /projections returns full metadata per topic
- DB query failure returns 503 with reason in body
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from scripts.projection_api_server import app, get_pool, get_topic_map

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat()


def _make_pool(rows: list[dict[str, Any]], latest_ts: str | None = None) -> MagicMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.fetchval = AsyncMock(return_value=latest_ts)

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


def _make_broken_pool() -> MagicMock:
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


def _make_cfg(
    topic: str = "test.topic.v1",
    table: str = "test_table",
    columns: tuple[str, ...] = ("col_a", "col_b"),
    order_by: str | None = "col_a DESC",
    freshness_column: str | None = "col_a",
    status: ProjectionStatus = ProjectionStatus.OK,
    degraded_reason: str = "",
) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table=table,
        schema_name="public",
        columns=columns,
        order_by=order_by,
        freshness_column=freshness_column,
        limit=100,
        source_contract="node_test",
        status=status,
        degraded_reason=degraded_reason,
    )


@contextmanager
def _with_overrides(
    pool: MagicMock,
    topic_map: dict[str, ProjectionTableConfig],
) -> Generator[TestClient, None, None]:
    """Override both get_pool and get_topic_map; yield a TestClient."""
    app.dependency_overrides[get_pool] = lambda: pool
    app.dependency_overrides[get_topic_map] = lambda: topic_map
    client = TestClient(app, raise_server_exceptions=True)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProjectionEndpointDynamic:
    def test_projection_endpoint_uses_declared_columns(self) -> None:
        """Response rows only include columns declared in the contract."""
        declared_cols = ("aggregation_key", "window", "total_cost_usd")
        cfg = _make_cfg(
            topic="onex.snapshot.projection.test.v1",
            columns=declared_cols,
            freshness_column="updated_at",
        )
        topic_map = {cfg.topic: cfg}
        # DB returns rows with those columns
        rows = [
            {
                "aggregation_key": "model-a",
                "window": "daily",
                "total_cost_usd": "1.23",
            }
        ]
        pool = _make_pool(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_overrides(pool, topic_map) as client:
            resp = client.get("/projection/onex.snapshot.projection.test.v1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 1
        row_keys = set(body["rows"][0].keys())
        assert row_keys == {"aggregation_key", "window", "total_cost_usd"}

    def test_degraded_table_returns_503(self) -> None:
        """A DEGRADED entry at startup returns 503 with the reason."""
        cfg = _make_cfg(
            topic="onex.snapshot.projection.test.v1",
            status=ProjectionStatus.DEGRADED,
            degraded_reason="table 'public.test_table' not found at startup",
        )
        topic_map = {cfg.topic: cfg}
        pool = _make_pool([])
        with _with_overrides(pool, topic_map) as client:
            resp = client.get("/projection/onex.snapshot.projection.test.v1")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert "not found" in body["reason"]

    def test_unknown_topic_returns_404_with_available_list(self) -> None:
        """Unknown topic returns 404 with all available topics listed."""
        cfg = _make_cfg(topic="known.topic.v1")
        topic_map = {"known.topic.v1": cfg}
        pool = _make_pool([])
        with _with_overrides(pool, topic_map) as client:
            resp = client.get("/projection/unknown.topic.v99")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "unknown_topic"
        assert "available_topics" in body
        assert "known.topic.v1" in body["available_topics"]

    def test_projections_endpoint_returns_full_metadata(self) -> None:
        """GET /projections returns metadata per topic, not just names."""
        cfg = _make_cfg(
            topic="onex.snapshot.projection.test.v1",
            table="test_table",
            columns=("col_a", "col_b"),
            order_by="col_a DESC",
            freshness_column="col_a",
        )
        topic_map = {cfg.topic: cfg}
        pool = _make_pool([])
        with _with_overrides(pool, topic_map) as client:
            resp = client.get("/projections")
        assert resp.status_code == 200
        body = resp.json()
        assert "topics" in body
        assert len(body["topics"]) == 1
        entry = body["topics"][0]
        assert entry["topic"] == "onex.snapshot.projection.test.v1"
        assert entry["table"] == "test_table"
        assert entry["status"] == "ok"
        assert set(entry["columns"]) == {"col_a", "col_b"}
        assert entry["order_by"] == "col_a DESC"
        assert entry["freshness_column"] == "col_a"
        assert entry["limit"] == 100
        assert entry["source_contract"] == "node_test"

    def test_query_failure_returns_503_degraded(self) -> None:
        """A DB query error during serving returns 503 with reason, not 500."""
        cfg = _make_cfg(topic="onex.snapshot.projection.test.v1")
        topic_map = {cfg.topic: cfg}
        pool = _make_broken_pool()
        with _with_overrides(pool, topic_map) as client:
            resp = client.get("/projection/onex.snapshot.projection.test.v1")
        assert resp.status_code == 503
        body = resp.json()
        assert body.get("status") == "degraded" or body.get("error") is not None

    def test_absent_order_by_returns_undefined_ordering(self) -> None:
        """When order_by is None, response includes ordering: undefined."""
        cfg = _make_cfg(
            topic="onex.snapshot.projection.test.v1",
            order_by=None,
            freshness_column="col_a",
        )
        topic_map = {cfg.topic: cfg}
        rows = [{"col_a": "v1", "col_b": "v2"}]
        pool = _make_pool(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_overrides(pool, topic_map) as client:
            resp = client.get("/projection/onex.snapshot.projection.test.v1")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ordering") == "undefined"

    def test_absent_freshness_column_returns_unknown_freshness(self) -> None:
        """When freshness_column is None, response data_freshness is 'unknown'."""
        cfg = _make_cfg(
            topic="onex.snapshot.projection.test.v1",
            freshness_column=None,
        )
        topic_map = {cfg.topic: cfg}
        pool = _make_pool([], latest_ts=None)
        with _with_overrides(pool, topic_map) as client:
            resp = client.get("/projection/onex.snapshot.projection.test.v1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_freshness"] == "unknown"
