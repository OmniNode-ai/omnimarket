"""Tests for projection_api_server (OMN-10461 / OMN-10490).

Covers:
- Unknown topic → 404 with available_topics list
- Response envelope shape for contract-driven topics
- Freshness computation (fresh / stale / degraded / unknown)
- correlation_id filter parameter is forwarded
- 503 when backing table is unreachable
- /health returns 200 with connectivity status
- /projections returns full metadata per topic
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from omnimarket.projection.models import ProjectionTableConfig
from scripts.projection_api_server import (
    app,
    compute_freshness,
    get_pool,
    get_topic_map,
)

# ---------------------------------------------------------------------------
# Canonical topic map matching the three contracts now in contract.yaml
# ---------------------------------------------------------------------------

_THREE_TOPIC_MAP: dict[str, ProjectionTableConfig] = {
    "onex.snapshot.projection.cost.summary.v1": ProjectionTableConfig(
        topic="onex.snapshot.projection.cost.summary.v1",
        table="llm_cost_aggregates",
        schema_name="public",
        columns=(
            "aggregation_key",
            "window",
            "total_cost_usd",
            "total_tokens",
            "call_count",
            "updated_at",
        ),
        order_by="updated_at DESC",
        freshness_column="updated_at",
        limit=100,
        source_contract="node_projection_cost_summary",
    ),
    "onex.snapshot.projection.cost.token_usage.v1": ProjectionTableConfig(
        topic="onex.snapshot.projection.cost.token_usage.v1",
        table="llm_call_metrics",
        schema_name="public",
        columns=(
            "model_id",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "usage_source",
            "created_at",
        ),
        order_by="created_at DESC",
        freshness_column="created_at",
        limit=100,
        source_contract="node_projection_cost_token_usage",
    ),
    "onex.snapshot.projection.registration.v1": ProjectionTableConfig(
        topic="onex.snapshot.projection.registration.v1",
        table="node_service_registry",
        schema_name="omnidash_analytics",
        columns=(
            "service_name",
            "service_type",
            "health_status",
            "is_active",
            "last_health_check",
            "updated_at",
            "projected_at",
        ),
        order_by="updated_at DESC",
        freshness_column="updated_at",
        limit=100,
        source_contract="projection_registration",
    ),
}

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
def _with_pool(
    pool: MagicMock,
    topic_map: dict[str, ProjectionTableConfig] | None = None,
) -> Generator[TestClient, None, None]:
    """Override get_pool (and optionally get_topic_map) and yield a TestClient."""
    effective_map = topic_map if topic_map is not None else _THREE_TOPIC_MAP
    app.dependency_overrides[get_pool] = lambda: pool
    app.dependency_overrides[get_topic_map] = lambda: effective_map
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
# Unit: contract-driven topic map invariants
# ---------------------------------------------------------------------------


class TestContractTopicMap:
    def test_all_three_topics_present(self) -> None:
        topics = set(_THREE_TOPIC_MAP.keys())
        assert "onex.snapshot.projection.cost.summary.v1" in topics
        assert "onex.snapshot.projection.cost.token_usage.v1" in topics
        assert "onex.snapshot.projection.registration.v1" in topics

    def test_no_select_star_in_columns(self) -> None:
        for topic, cfg in _THREE_TOPIC_MAP.items():
            assert "*" not in cfg.columns, f"{topic} uses SELECT *"

    def test_limit_is_100(self) -> None:
        for topic, cfg in _THREE_TOPIC_MAP.items():
            assert cfg.limit == 100, f"{topic} limit != 100"

    def test_all_topics_have_order_by(self) -> None:
        for topic, cfg in _THREE_TOPIC_MAP.items():
            if cfg.order_by is None or cfg.order_by == "undefined":
                continue
            assert cfg.order_by is not None, f"{topic} missing order_by"

    def test_all_topics_have_freshness_column(self) -> None:
        for topic, cfg in _THREE_TOPIC_MAP.items():
            if cfg.freshness_column is None or cfg.freshness_column == "unknown":
                continue
            assert cfg.freshness_column is not None, f"{topic} missing freshness_column"


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
        assert set(body["available_topics"]) == set(_THREE_TOPIC_MAP.keys())

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
                "service_name": "node-abc",
                "service_type": "COMPUTE",
                "health_status": "active",
                "is_active": True,
                "last_health_check": _ts(timedelta(minutes=10)),
                "updated_at": _ts(timedelta(minutes=10)),
                "projected_at": _ts(timedelta(minutes=10)),
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
        assert (
            body.get("status") == "degraded"
            or body.get("error") == "upstream_unavailable"
        )

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
        call_args = conn.fetch.call_args
        assert "FROM public.llm_cost_aggregates" in call_args[0][0]
        assert "corr-abc" in call_args[0]

    def test_queries_use_configured_schema(self) -> None:
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=None)

        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)

        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.registration.v1")

        assert resp.status_code == 200
        sql = conn.fetch.call_args[0][0]
        assert "FROM omnidash_analytics.node_service_registry" in sql
        freshness_sql = conn.fetchval.call_args[0][0]
        assert "FROM omnidash_analytics.node_service_registry" in freshness_sql


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


class TestProjectionsListRoute:
    def test_projections_returns_metadata_for_all_topics(self) -> None:
        pool = _make_pool([])
        with _with_pool(pool) as client:
            resp = client.get("/projections")
        assert resp.status_code == 200
        body = resp.json()
        assert "topics" in body
        assert len(body["topics"]) == 3
        topic_names = {t["topic"] for t in body["topics"]}
        assert topic_names == set(_THREE_TOPIC_MAP.keys())

    def test_projections_entry_has_required_fields(self) -> None:
        pool = _make_pool([])
        with _with_pool(pool) as client:
            resp = client.get("/projections")
        body = resp.json()
        for entry in body["topics"]:
            assert "topic" in entry
            assert "table" in entry
            assert "status" in entry
            assert "columns" in entry
            assert "limit" in entry
            assert "source_contract" in entry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_envelope(body: dict[str, Any], topic: str) -> None:
    assert body["topic"] == topic
    assert "projection_version" in body
    assert "generated_at" in body
    assert "data_freshness" in body
    assert body["data_freshness"] in {"fresh", "stale", "degraded", "unknown"}
    assert "row_count" in body
    assert "rows" in body
    assert isinstance(body["rows"], list)
