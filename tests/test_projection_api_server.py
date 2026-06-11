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
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from omnimarket.config.settings import Settings
from omnimarket.projection.models import ProjectionTableConfig
from scripts.projection_api_server import (
    PROJECTION_DATABASE_BINDING_OVERLAY_ENV,
    ModelProjectionDatabaseBinding,
    _cors_origins_from_env,
    _dsn,
    app,
    compute_freshness,
    get_pool,
    get_topic_map,
    load_projection_database_binding_overlay,
)

# ---------------------------------------------------------------------------
# Canonical topic map matching the contracts exposed through projection_api.
# ---------------------------------------------------------------------------

_PROJECTION_TOPIC_MAP: dict[str, ProjectionTableConfig] = {
    "onex.snapshot.projection.ab-compare.v1": ProjectionTableConfig(
        topic="onex.snapshot.projection.ab-compare.v1",
        table="llm_call_metrics",
        schema_name="public",
        columns=(
            "correlation_id",
            "model_id",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "latency_ms",
            "usage_source",
            "created_at",
        ),
        order_by="created_at DESC",
        freshness_column="created_at",
        limit=100,
        source_contract="ab_compare_reducer",
    ),
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
    effective_map = topic_map if topic_map is not None else _PROJECTION_TOPIC_MAP
    app.dependency_overrides[get_pool] = lambda: pool
    app.dependency_overrides[get_topic_map] = lambda: effective_map
    client = TestClient(app, raise_server_exceptions=True)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Unit: DSN construction
# ---------------------------------------------------------------------------


class TestPostgresDsn:
    def test_dsn_requires_projection_database_binding_or_compat_settings(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
        monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)

        with pytest.raises(
            RuntimeError,
            match="projection database binding is required",
        ):
            _dsn(settings=Settings(_env_file=None))  # type: ignore[call-arg]

    def test_dsn_uses_explicit_binding_without_db_url_env(self, monkeypatch) -> None:
        expected = "postgresql://projection:pw@db.internal:15436/omnidash_analytics"
        monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
        monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)
        binding = ModelProjectionDatabaseBinding(database_url=SecretStr(expected))

        assert _dsn(binding=binding, settings=Settings(_env_file=None)) == expected  # type: ignore[call-arg]

    def test_dsn_loads_overlay_file_without_db_url_env(
        self, monkeypatch, tmp_path
    ) -> None:
        expected = "postgresql://projection:pw@db.internal:15436/omnidash_analytics"
        monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
        monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)
        overlay_path = tmp_path / "projection-db-binding.yaml"
        overlay_path.write_text(f'database_url: "{expected}"\n', encoding="utf-8")

        assert (
            _dsn(overlay_path=overlay_path, settings=Settings(_env_file=None))
            == expected
        )  # type: ignore[call-arg]

    def test_dsn_loads_overlay_from_selector_env(self, monkeypatch, tmp_path) -> None:
        expected = "postgresql://projection:pw@db.internal:15436/omnidash_analytics"
        monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
        monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)
        overlay_path = tmp_path / "projection-db-binding.yaml"
        overlay_path.write_text(f'database_url: "{expected}"\n', encoding="utf-8")
        monkeypatch.setenv(PROJECTION_DATABASE_BINDING_OVERLAY_ENV, str(overlay_path))

        assert _dsn() == expected

    def test_dsn_overlay_secret_ref_uses_secret_resolver(
        self, monkeypatch, tmp_path
    ) -> None:
        expected = "postgresql://projection:pw@db.internal:15436/omnidash_analytics"
        monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
        monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)
        overlay_path = tmp_path / "projection-db-binding.yaml"
        overlay_path.write_text(
            'database_url_secret_ref: "secret/omnidash-analytics-db-url"\n',
            encoding="utf-8",
        )

        def _resolve_secret_ref(secret_ref: str | None, *, required: bool) -> SecretStr:
            assert secret_ref == "secret/omnidash-analytics-db-url"
            assert required is True
            return SecretStr(expected)

        monkeypatch.setattr(
            "omnimarket.projection.api_server.resolve_secret_ref",
            _resolve_secret_ref,
        )

        assert (
            _dsn(overlay_path=overlay_path, settings=Settings(_env_file=None))
            == expected
        )  # type: ignore[call-arg]

    def test_projection_database_binding_overlay_requires_one_url_source(
        self, tmp_path
    ) -> None:
        overlay_path = tmp_path / "projection-db-binding.yaml"
        overlay_path.write_text("database_url: ''\n", encoding="utf-8")

        with pytest.raises(ValueError, match="projection database_url"):
            load_projection_database_binding_overlay(overlay_path)

    def test_dsn_settings_compat_prefers_analytics_db_url(self) -> None:
        expected = "postgresql://projection:pw@db.internal:15436/omnidash_analytics"
        fallback = "postgresql://projection:pw@db.internal:15436/omnibase_infra"
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            omnidash_analytics_db_url=SecretStr(expected),
            omnibase_infra_db_url=SecretStr(fallback),
        )

        assert _dsn(settings=settings) == expected

    def test_dsn_settings_compat_falls_back_to_contract_db_url(
        self, monkeypatch
    ) -> None:
        expected = "postgresql://projection:pw@db.internal:15436/omnibase_infra"
        monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
        monkeypatch.delenv("OMNIBASE_INFRA_DB_URL", raising=False)
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            omnibase_infra_db_url=SecretStr(expected),
        )

        assert _dsn(settings=settings) == expected


class TestCorsConfiguration:
    def test_projection_api_cors_origins_use_projection_specific_env(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv(
            "PROJECTION_API_CORS_ORIGINS",
            "http://localhost:5173, https://dash.example.com ",
        )
        monkeypatch.setenv("CORS_ORIGINS", "https://registry.example.com")

        assert _cors_origins_from_env() == [
            "http://localhost:5173",
            "https://dash.example.com",
        ]

    def test_projection_api_cors_origins_fall_back_to_shared_env(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("PROJECTION_API_CORS_ORIGINS", raising=False)
        monkeypatch.setenv("CORS_ORIGINS", "https://dash.example.com")

        assert _cors_origins_from_env() == ["https://dash.example.com"]


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
    def test_exposed_topics_present(self) -> None:
        topics = set(_PROJECTION_TOPIC_MAP.keys())
        assert "onex.snapshot.projection.ab-compare.v1" in topics
        assert "onex.snapshot.projection.cost.summary.v1" in topics
        assert "onex.snapshot.projection.cost.token_usage.v1" in topics
        assert "onex.snapshot.projection.registration.v1" in topics

    def test_no_select_star_in_columns(self) -> None:
        for topic, cfg in _PROJECTION_TOPIC_MAP.items():
            assert "*" not in cfg.columns, f"{topic} uses SELECT *"

    def test_limit_is_100(self) -> None:
        for topic, cfg in _PROJECTION_TOPIC_MAP.items():
            assert cfg.limit == 100, f"{topic} limit != 100"

    def test_all_topics_have_order_by(self) -> None:
        for topic, cfg in _PROJECTION_TOPIC_MAP.items():
            if cfg.order_by is None or cfg.order_by == "undefined":
                continue
            assert cfg.order_by is not None, f"{topic} missing order_by"

    def test_all_topics_have_freshness_column(self) -> None:
        for topic, cfg in _PROJECTION_TOPIC_MAP.items():
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
        assert set(body["available_topics"]) == set(_PROJECTION_TOPIC_MAP.keys())

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

    def test_ab_compare_envelope_shape_uses_llm_call_metrics_fields(self) -> None:
        rows = [
            {
                "correlation_id": "run-abc",
                "model_id": "qwen3-coder-30b",
                "prompt_tokens": 100,
                "completion_tokens": 200,
                "total_tokens": 300,
                "estimated_cost_usd": "0.0025",
                "latency_ms": "812.5",
                "usage_source": "actual",
                "created_at": _ts(timedelta(minutes=2)),
            }
        ]
        pool = _make_pool(rows, latest_ts=_ts(timedelta(minutes=2)))
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.ab-compare.v1")
        assert resp.status_code == 200
        body = resp.json()
        _assert_envelope(body, "onex.snapshot.projection.ab-compare.v1")
        assert body["row_count"] == 1
        assert body["rows"][0]["correlation_id"] == "run-abc"
        assert body["rows"][0]["model_id"] == "qwen3-coder-30b"
        assert body["rows"][0]["estimated_cost_usd"] == "0.0025"

    def test_delegation_projection_api_preserves_manifest_pricing_fields(
        self,
    ) -> None:
        topic = "onex.evt.omnimarket.projection-delegation-events.v1"
        cfg = ProjectionTableConfig(
            topic=topic,
            table="delegation_events",
            schema_name="public",
            columns=(
                "correlation_id",
                "task_type",
                "delegated_to",
                "cost_savings_usd",
                "pricing_manifest_version",
                "timestamp",
            ),
            order_by="timestamp DESC",
            freshness_column="timestamp",
            limit=100,
            source_contract="projection_delegation",
        )
        rows = [
            {
                "correlation_id": "corr-pricing-proof",
                "task_type": "test",
                "delegated_to": "Qwen3-Coder-30B-A3B",
                "cost_savings_usd": Decimal("0.00525"),
                "pricing_manifest_version": 1,
                "timestamp": _ts(timedelta(minutes=1)),
            }
        ]
        pool = _make_pool(rows, latest_ts=_ts(timedelta(minutes=1)))

        with _with_pool(pool, topic_map={topic: cfg}) as client:
            resp = client.get(
                f"/projection/{topic}",
                params={"correlation_id": "corr-pricing-proof"},
            )

        assert resp.status_code == 200
        body = resp.json()
        _assert_envelope(body, topic)
        assert body["row_count"] == 1
        row = body["rows"][0]
        assert row["correlation_id"] == "corr-pricing-proof"
        assert row["cost_savings_usd"] == "0.00525"
        assert row["pricing_manifest_version"] == 1

    def test_raw_delegation_topic_serialises_uuid_columns(self) -> None:
        """A raw projection row exposing a UUID column must serialise to its
        string form without raising (regression: OMN-12558).

        ``public.delegation_events`` has UUID column(s); a populated row crashed
        ``json.dumps`` with ``TypeError: Object of type UUID is not JSON
        serializable`` because ``_json_value`` had no UUID branch. datetime and
        Decimal are exercised alongside it to assert full typed serialisation.
        """
        topic = "delegation"
        cfg = ProjectionTableConfig(
            topic=topic,
            table="delegation_events",
            schema_name="public",
            columns=(
                "id",
                "correlation_id",
                "cost_savings_usd",
                "created_at",
            ),
            order_by="created_at DESC",
            freshness_column="created_at",
            limit=100,
            source_contract="projection_delegation",
        )
        event_id = UUID("12345678-1234-5678-1234-567812345678")
        corr_id = UUID("87654321-4321-8765-4321-876543218765")
        created = datetime.now(UTC) - timedelta(minutes=1)
        rows = [
            {
                "id": event_id,
                "correlation_id": corr_id,
                "cost_savings_usd": Decimal("0.00525"),
                "created_at": created,
            }
        ]
        pool = _make_pool(rows, latest_ts=created.isoformat())

        with _with_pool(pool, topic_map={topic: cfg}) as client:
            resp = client.get(f"/projection/{topic}")

        assert resp.status_code == 200
        body = resp.json()
        _assert_envelope(body, topic)
        assert body["row_count"] == 1
        row = body["rows"][0]
        assert row["id"] == str(event_id)
        assert row["correlation_id"] == str(corr_id)
        assert row["cost_savings_usd"] == "0.00525"
        assert row["created_at"] == created.isoformat()

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

    def test_json_columns_are_decoded_for_dashboard_projection_rows(self) -> None:
        topic = "onex.snapshot.projection.delegation.model-routing.v1"
        cfg = ProjectionTableConfig(
            topic=topic,
            table="projection_delegation_model_routing",
            schema_name="public",
            columns=(
                "total_delegations",
                "rows",
                "by_model",
                "decision_traces",
                "latest_projection_updated_at",
            ),
            json_columns=("rows", "by_model", "decision_traces"),
            freshness_column="latest_projection_updated_at",
            limit=1,
            source_contract="projection_delegation",
        )
        rows = [
            {
                "total_delegations": 1,
                "rows": '[{"model_name":"qwen","task_type":"test","count":1}]',
                "by_model": '[{"model_name":"qwen","total_count":1}]',
                "decision_traces": '[{"correlation_id":"corr-json"}]',
                "latest_projection_updated_at": _ts(timedelta(minutes=1)),
            }
        ]
        pool = _make_pool(rows, latest_ts=_ts(timedelta(minutes=1)))

        with _with_pool(pool, topic_map={topic: cfg}) as client:
            resp = client.get(f"/projection/{topic}")

        assert resp.status_code == 200
        body = resp.json()
        _assert_envelope(body, topic)
        assert body["rows"][0]["rows"][0]["model_name"] == "qwen"
        assert body["rows"][0]["by_model"][0]["total_count"] == 1
        assert body["rows"][0]["decision_traces"][0]["correlation_id"] == "corr-json"

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

    def test_ab_compare_correlation_id_filter_targets_llm_call_metrics(self) -> None:
        """AB Compare projection forwards correlation_id against llm_call_metrics."""
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
                "/projection/onex.snapshot.projection.ab-compare.v1",
                params={"correlation_id": "run-abc"},
            )
        assert resp.status_code == 200
        call_args = conn.fetch.call_args
        assert "FROM public.llm_call_metrics" in call_args[0][0]
        assert "WHERE correlation_id = $1" in call_args[0][0]
        assert "run-abc" in call_args[0]

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
        assert len(body["topics"]) == 4
        topic_names = {t["topic"] for t in body["topics"]}
        assert topic_names == set(_PROJECTION_TOPIC_MAP.keys())

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


# ---------------------------------------------------------------------------
# POST /api/generate — thin publisher route (OMN-13004)
# ---------------------------------------------------------------------------


class TestGenerateRoute:
    def test_generate_publishes_and_returns_correlation_id(self, monkeypatch) -> None:
        import omnimarket.projection.api_server as api
        from omnimarket.projection.generation_publisher import (
            NODE_GENERATION_REQUESTED_TOPIC,
            ModelGenerateRequest,
            ModelGenerateResponse,
        )

        seen: dict[str, Any] = {}

        async def _fake_publish(
            request: ModelGenerateRequest,
        ) -> ModelGenerateResponse:
            seen["task_description"] = request.task_description
            return ModelGenerateResponse(
                correlation_id="ui-20260611T120000Z-abcd1234",
                topic=NODE_GENERATION_REQUESTED_TOPIC,
            )

        monkeypatch.setattr(api, "publish_generation_request", _fake_publish)

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/api/generate",
            json={"task_description": "Generate a node that adds two ints"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["correlation_id"] == "ui-20260611T120000Z-abcd1234"
        assert body["topic"] == NODE_GENERATION_REQUESTED_TOPIC
        assert seen["task_description"] == "Generate a node that adds two ints"

    def test_generate_rejects_empty_task_description(self) -> None:
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/generate", json={"task_description": ""})
        assert resp.status_code == 422

    def test_generate_returns_503_when_broker_unconfigured(self, monkeypatch) -> None:
        import omnimarket.projection.api_server as api
        from omnimarket.projection.generation_publisher import ModelGenerateRequest

        async def _raise(request: ModelGenerateRequest) -> None:
            raise RuntimeError("KAFKA_BOOTSTRAP_SERVERS is required")

        monkeypatch.setattr(api, "publish_generation_request", _raise)

        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/generate", json={"task_description": "x"})
        assert resp.status_code == 503
        assert "KAFKA_BOOTSTRAP_SERVERS" in resp.json()["detail"]
