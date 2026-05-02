"""Unit tests for projection topic map immutability (OMN-10490).

Tests:
- Startup snapshot is not mutated after lifespan completes
- DEGRADED entry persists across requests without re-checking DB
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from scripts.projection_api_server import app, get_pool, get_topic_map

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_degraded_cfg(topic: str = "test.topic.v1") -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table="missing_table",
        schema_name="public",
        columns=("col_a",),
        order_by=None,
        freshness_column=None,
        limit=100,
        source_contract="node_test",
        status=ProjectionStatus.DEGRADED,
        degraded_reason="table 'public.missing_table' not found at startup",
    )


def _make_ok_cfg(topic: str = "test.topic.v1") -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table="present_table",
        schema_name="public",
        columns=("col_a",),
        order_by="col_a DESC",
        freshness_column="col_a",
        limit=100,
        source_contract="node_test",
        status=ProjectionStatus.OK,
        degraded_reason="",
    )


@contextmanager
def _with_overrides(
    pool: MagicMock,
    topic_map: dict[str, ProjectionTableConfig],
) -> Generator[TestClient, None, None]:
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


class TestStartupSnapshotImmutable:
    def test_topic_map_object_is_frozen(self) -> None:
        """ProjectionTableConfig is frozen=True — mutations raise an error."""
        from pydantic import ValidationError

        cfg = _make_ok_cfg()
        try:
            cfg.status = ProjectionStatus.DEGRADED  # type: ignore[misc]
            raise AssertionError("Expected error from frozen model")
        except (TypeError, AttributeError, ValidationError):
            pass  # expected — model is immutable

    def test_topic_map_dict_is_not_modified_by_requests(self) -> None:
        """Multiple requests do not modify the topic_map dict or its entries."""
        topic = "test.topic.v1"
        cfg = _make_ok_cfg(topic)
        topic_map: dict[str, ProjectionTableConfig] = {topic: cfg}
        original_cfg_id = id(topic_map[topic])
        original_map_len = len(topic_map)

        pool = _make_pool([{"col_a": "value"}], latest_ts=None)
        with _with_overrides(pool, topic_map) as client:
            for _ in range(3):
                client.get(f"/projection/{topic}")

        # Map length unchanged
        assert len(topic_map) == original_map_len
        # Same config object in the map (not replaced)
        assert id(topic_map[topic]) == original_cfg_id
        # Config still OK
        assert topic_map[topic].status == ProjectionStatus.OK


class TestDegradedEntryPersists:
    def test_degraded_entry_returns_503_on_every_request(self) -> None:
        """Once marked DEGRADED at startup, every subsequent request returns 503."""
        topic = "test.degraded.topic.v1"
        cfg = _make_degraded_cfg(topic)
        topic_map = {topic: cfg}
        pool = _make_pool([])

        with _with_overrides(pool, topic_map) as client:
            for _ in range(3):
                resp = client.get(f"/projection/{topic}")
                assert resp.status_code == 503, (
                    f"Expected 503 on every request, got {resp.status_code}"
                )

    def test_degraded_reason_preserved_in_response(self) -> None:
        """The degraded_reason from startup is returned verbatim in 503 body."""
        topic = "test.degraded.topic.v1"
        expected_reason = "table 'public.missing_table' not found at startup"
        cfg = _make_degraded_cfg(topic)
        assert cfg.degraded_reason == expected_reason

        topic_map = {topic: cfg}
        pool = _make_pool([])

        with _with_overrides(pool, topic_map) as client:
            resp = client.get(f"/projection/{topic}")

        assert resp.status_code == 503
        body = resp.json()
        assert body.get("reason") == expected_reason

    def test_degraded_entry_does_not_recheck_db(self) -> None:
        """A DEGRADED entry never issues a DB query — pool.acquire is not called."""
        topic = "test.degraded.topic.v1"
        cfg = _make_degraded_cfg(topic)
        topic_map = {topic: cfg}

        pool = MagicMock()
        pool.acquire = MagicMock()

        with _with_overrides(pool, topic_map) as client:
            client.get(f"/projection/{topic}")

        pool.acquire.assert_not_called()
