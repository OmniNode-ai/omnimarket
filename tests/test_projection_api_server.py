"""Tests for projection_api_server (OMN-10461).

Covers:
- Whitelist enforcement (unknown topic → 404)
- Response envelope shape for each whitelisted topic
- Freshness computation (fresh / stale / degraded)
- correlation_id filter parameter is forwarded
- 503 when backing table is unreachable
- /health returns 200 with connectivity status
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from scripts.projection_api_server import (
    TOPIC_WHITELIST,
    app,
    compute_freshness,
    get_pool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat()


def _make_pool(rows: list[dict[str, Any]], latest_ts: str | None = None) -> MagicMock:
    """Build a mock asyncpg pool whose connections return `rows` on fetch."""
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
    """Pool whose acquire raises on entry (simulates DB unreachable)."""
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


@contextmanager
def _with_pool(pool: MagicMock) -> Generator[TestClient, None, None]:
    """Override the get_pool dependency and yield a TestClient (no lifespan)."""
    app.dependency_overrides[get_pool] = lambda: pool
    # Use raise_server_exceptions=True (default) but disable lifespan via
    # app_state injection — simplest approach is to patch _pool directly and
    # skip the lifespan by using TestClient without the context manager.
    client = TestClient(app, raise_server_exceptions=True)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Unit: freshness computation (pure function, no DB)
# ---------------------------------------------------------------------------


class TestComputeFreshness:
    def test_fresh_within_5_min(self) -> None:
        assert compute_freshness(_ts(timedelta(minutes=2))) == "fresh"

    def test_stale_between_5_and_60_min(self) -> None:
        assert compute_freshness(_ts(timedelta(minutes=30))) == "stale"

    def test_degraded_older_than_60_min(self) -> None:
        assert compute_freshness(_ts(timedelta(hours=2))) == "degraded"

    def test_none_returns_degraded(self) -> None:
        assert compute_freshness(None) == "degraded"


# ---------------------------------------------------------------------------
# Unit: whitelist invariants
# ---------------------------------------------------------------------------


class TestWhitelist:
    def test_all_three_topics_present(self) -> None:
        topics = set(TOPIC_WHITELIST.keys())
        assert "onex.snapshot.projection.cost.summary.v1" in topics
        assert "onex.snapshot.projection.cost.token_usage.v1" in topics
        assert "onex.snapshot.projection.registration.v1" in topics

    def test_no_select_star_in_columns(self) -> None:
        for topic, cfg in TOPIC_WHITELIST.items():
            assert "*" not in cfg["columns"], f"{topic} uses SELECT *"

    def test_limit_is_100(self) -> None:
        for topic, cfg in TOPIC_WHITELIST.items():
            assert cfg.get("limit", 100) == 100, f"{topic} limit != 100"


# ---------------------------------------------------------------------------
# Route tests — dependency_overrides so no real DB or lifespan needed
# ---------------------------------------------------------------------------


class TestProjectionRoutes:
    def test_unknown_topic_returns_404(self) -> None:
        pool = _make_pool([])
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.does.not.exist.v1")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "unknown_topic"
        assert "available_topics" in body
        assert set(body["available_topics"]) == set(TOPIC_WHITELIST.keys())

    def test_cost_summary_envelope_shape(self) -> None:
        rows = [
            {
                "aggregation_key": "model-a",
                "window": "daily",
                "total_cost_usd": "1.23",
                "total_tokens": 1000,
                "call_count": 5,
                "updated_at": _ts(timedelta(minutes=1)),
            }
        ]
        pool = _make_pool(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.status_code == 200
        _assert_envelope(resp.json(), "onex.snapshot.projection.cost.summary.v1")
        assert resp.json()["row_count"] == 1

    def test_token_usage_envelope_shape(self) -> None:
        rows = [
            {
                "model_id": "claude-opus",
                "prompt_tokens": 100,
                "completion_tokens": 200,
                "total_tokens": 300,
                "estimated_cost_usd": "0.05",
                "usage_source": "direct",
                "created_at": _ts(timedelta(minutes=3)),
            }
        ]
        pool = _make_pool(rows, latest_ts=_ts(timedelta(minutes=3)))
        with _with_pool(pool) as client:
            resp = client.get(
                "/projection/onex.snapshot.projection.cost.token_usage.v1"
            )
        assert resp.status_code == 200
        _assert_envelope(resp.json(), "onex.snapshot.projection.cost.token_usage.v1")
        assert resp.json()["row_count"] == 1

    def test_registration_envelope_shape(self) -> None:
        rows = [
            {
                "entity_id": "node-abc",
                "domain": "omnimarket",
                "current_state": "active",
                "node_type": "COMPUTE",
                "node_version": "1.0.0",
                "registered_at": _ts(timedelta(minutes=10)),
            }
        ]
        pool = _make_pool(rows, latest_ts=_ts(timedelta(minutes=10)))
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.registration.v1")
        assert resp.status_code == 200
        _assert_envelope(resp.json(), "onex.snapshot.projection.registration.v1")
        assert resp.json()["row_count"] == 1

    def test_freshness_fresh(self) -> None:
        pool = _make_pool([], latest_ts=_ts(timedelta(minutes=1)))
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.json()["data_freshness"] == "fresh"

    def test_freshness_stale(self) -> None:
        pool = _make_pool([], latest_ts=_ts(timedelta(minutes=30)))
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.json()["data_freshness"] == "stale"

    def test_freshness_degraded(self) -> None:
        pool = _make_pool([], latest_ts=_ts(timedelta(hours=2)))
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.json()["data_freshness"] == "degraded"

    def test_upstream_unavailable_returns_503(self) -> None:
        pool = _make_broken_pool()
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "upstream_unavailable"
        assert body["data_freshness"] == "degraded"

    def test_correlation_id_filter_forwarded(self) -> None:
        """correlation_id query param is forwarded as a SQL positional arg."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=None)

        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)

        with _with_pool(pool) as client:
            resp = client.get(
                "/projection/onex.snapshot.projection.cost.summary.v1",
                params={"correlation_id": "corr-abc"},
            )
        assert resp.status_code == 200
        # Verify conn.fetch was called and "corr-abc" was passed as positional arg
        call_args = conn.fetch.call_args
        assert "corr-abc" in call_args[0]


class TestHealthRoute:
    def test_health_returns_200_when_ok(self) -> None:
        pool = _make_pool([])
        with _with_pool(pool) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "postgres" in body
        assert body["postgres"] == "ok"

    def test_health_reflects_postgres_unreachable(self) -> None:
        pool = _make_broken_pool()
        with _with_pool(pool) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["postgres"] == "unreachable"
        assert body["status"] == "degraded"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_envelope(body: dict[str, Any], topic: str) -> None:
    assert body["topic"] == topic
    assert "projection_version" in body
    assert "generated_at" in body
    assert "data_freshness" in body
    assert body["data_freshness"] in {"fresh", "stale", "degraded"}
    assert "row_count" in body
    assert "rows" in body
    assert isinstance(body["rows"], list)
