"""Unit tests for projection topic map immutability (OMN-10490 / OMN-15800).

Tests:
- Startup snapshot is not mutated after lifespan completes
- DEGRADED entry persists across requests without ever reaching the cache
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from scripts.projection_api_server import app, get_snapshot_cache, get_topic_map

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cache() -> MagicMock:
    cache = MagicMock()
    cache.is_bootstrapped = MagicMock(return_value=True)
    cache.get_rows = MagicMock(return_value=[])
    cache.latest_event_at = MagicMock(return_value=None)
    return cache


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
        order_by_spec=(("col_a", "DESC", None),),
        freshness_column="col_a",
        limit=100,
        source_contract="node_test",
        status=ProjectionStatus.OK,
        degraded_reason="",
        bus_backed=True,
        key_columns=("col_a",),
    )


@contextmanager
def _with_overrides(
    cache: MagicMock,
    topic_map: dict[str, ProjectionTableConfig],
) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
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

        cache = _make_cache()
        cache.get_rows = MagicMock(return_value=[{"col_a": "value"}])
        with _with_overrides(cache, topic_map) as client:
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
        cache = _make_cache()

        with _with_overrides(cache, topic_map) as client:
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
        cache = _make_cache()

        with _with_overrides(cache, topic_map) as client:
            resp = client.get(f"/projection/{topic}")

        assert resp.status_code == 503
        body = resp.json()
        assert body.get("reason") == expected_reason

    def test_degraded_entry_never_reaches_cache(self) -> None:
        """A DEGRADED entry never touches the SnapshotCache — get_rows not called."""
        topic = "test.degraded.topic.v1"
        cfg = _make_degraded_cfg(topic)
        topic_map = {topic: cfg}
        cache = _make_cache()

        with _with_overrides(cache, topic_map) as client:
            client.get(f"/projection/{topic}")

        cache.get_rows.assert_not_called()
