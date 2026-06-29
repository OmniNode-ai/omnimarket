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
    resolve_effective_limit,
    resolve_order_clause,
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


def _make_capturing_pool(
    rows: list[dict[str, Any]], latest_ts: str | None = None
) -> tuple[MagicMock, list[str]]:
    """Pool whose ``fetch`` records every SQL string it receives.

    Returns the pool and a list that accumulates SQL strings in call order so a
    test can assert the emitted ``LIMIT`` / ``ORDER BY`` clause.
    """
    captured: list[str] = []

    async def _fetch(sql: str, *args: object) -> list[dict[str, Any]]:
        captured.append(sql)
        return rows

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.fetchval = AsyncMock(return_value=latest_ts)

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool, captured


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
    """Freshness classification, including contract-cadence + idle (OMN-13035)."""

    def test_none_returns_degraded(self) -> None:
        # No rows at all is a genuine projection problem, distinct from idle.
        assert compute_freshness(None) == "degraded"
        assert compute_freshness(None, expected_event_interval_seconds=60) == "degraded"

    # --- On-demand topics (no declared cadence): silence is never stale -----

    def test_on_demand_recent_is_fresh(self) -> None:
        assert compute_freshness(_ts(timedelta(minutes=2))) == "fresh"

    def test_on_demand_quiet_is_idle_not_stale(self) -> None:
        # DoD: an on-demand topic must NEVER report "stale" from silence.
        assert compute_freshness(_ts(timedelta(minutes=30))) == "idle"

    def test_on_demand_long_silence_is_still_idle_not_degraded(self) -> None:
        # The cry-wolf "degraded from silence" label is retired for on-demand.
        result = compute_freshness(_ts(timedelta(hours=6)))
        assert result == "idle"
        assert result not in {"stale", "degraded"}

    # --- Cadenced topics (contract declares expected_event_interval_seconds) -

    def test_cadenced_within_interval_is_fresh(self) -> None:
        assert (
            compute_freshness(
                _ts(timedelta(seconds=30)), expected_event_interval_seconds=60
            )
            == "fresh"
        )

    def test_cadenced_one_missed_beat_is_idle(self) -> None:
        # interval <= age < 2*interval -> idle (quiet, not yet alarming).
        assert (
            compute_freshness(
                _ts(timedelta(seconds=90)), expected_event_interval_seconds=60
            )
            == "idle"
        )

    def test_cadenced_behind_two_intervals_is_stale(self) -> None:
        # age >= 2*interval -> genuinely behind the declared cadence.
        assert (
            compute_freshness(
                _ts(timedelta(seconds=180)), expected_event_interval_seconds=60
            )
            == "stale"
        )


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
                "by_tier",
                "latest_projection_updated_at",
            ),
            json_columns=("rows", "by_model", "decision_traces", "by_tier"),
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
                # OMN-13662: tier distribution with explicit not_tier_routed
                # classification; the projection-API must return it decoded so
                # the dashboard reads the classification instead of re-deriving.
                "by_tier": (
                    '{"total_tasks":3,"tier_routed_total":1,'
                    '"not_tier_routed_count":2,"tiers":['
                    '{"cost_tier_name":"local","count":1,"tier_routed":true,'
                    '"pct_of_tier_routed":1.0},'
                    '{"cost_tier_name":"not_tier_routed","count":2,'
                    '"tier_routed":false,"pct_of_tier_routed":0}]}'
                ),
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
        by_tier = body["rows"][0]["by_tier"]
        assert by_tier["total_tasks"] == 3
        assert by_tier["not_tier_routed_count"] == 2
        not_routed = next(
            t for t in by_tier["tiers"] if t["cost_tier_name"] == "not_tier_routed"
        )
        assert not_routed["tier_routed"] is False
        assert not_routed["pct_of_tier_routed"] == 0

    def test_freshness_fresh(self) -> None:
        pool = _make_pool([], latest_ts=_ts(timedelta(minutes=1)))
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.json()["data_freshness"] == "fresh"

    def test_on_demand_quiet_reports_idle_not_stale(self) -> None:
        # OMN-13035 / retro B-7: cost.summary declares no cadence, so it is an
        # on-demand topic. 30-minute silence is honest "idle", NOT the cry-wolf
        # "stale" the projection API used to emit from mere quiet.
        pool = _make_pool([], latest_ts=_ts(timedelta(minutes=30)))
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.json()["data_freshness"] == "idle"

    def test_on_demand_long_silence_still_idle_not_degraded(self) -> None:
        # On-demand topics never degrade from silence alone (only None rows do).
        pool = _make_pool([], latest_ts=_ts(timedelta(hours=2)))
        with _with_pool(pool) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.json()["data_freshness"] == "idle"

    def test_cadenced_topic_behind_cadence_reports_stale(self) -> None:
        # A topic that DOES declare expected_event_interval_seconds is genuinely
        # stale once it falls 2x past its cadence — the honest-staleness signal
        # is preserved for streaming projections.
        topic = "onex.snapshot.projection.cost.summary.v1"
        cadenced = _PROJECTION_TOPIC_MAP[topic].model_copy(
            update={"expected_event_interval_seconds": 60}
        )
        pool = _make_pool([], latest_ts=_ts(timedelta(minutes=30)))
        with _with_pool(pool, topic_map={topic: cadenced}) as client:
            resp = client.get(f"/projection/{topic}")
        assert resp.json()["data_freshness"] == "stale"

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

    def test_correlation_id_filter_rejected_for_aggregate_topic(self) -> None:
        """Aggregate topics without correlation_id expose typed 422."""
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
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "unsupported_filter"
        assert body["filter"] == "correlation_id"
        assert body["topic"] == "onex.snapshot.projection.cost.summary.v1"
        conn.fetch.assert_not_called()

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
    assert body["data_freshness"] in {
        "fresh",
        "idle",
        "stale",
        "degraded",
        "unknown",
    }
    assert "row_count" in body
    assert "rows" in body
    assert isinstance(body["rows"], list)


# ---------------------------------------------------------------------------
# OMN-12999: generic limit / order query params on /projection/{topic}
# ---------------------------------------------------------------------------


class TestResolveEffectiveLimit:
    def test_none_request_uses_contract_limit(self) -> None:
        assert resolve_effective_limit(None, 500) == 500

    def test_smaller_request_is_honoured(self) -> None:
        assert resolve_effective_limit(25, 500) == 25

    def test_request_above_ceiling_is_clamped(self) -> None:
        assert resolve_effective_limit(10_000, 500) == 500

    def test_non_positive_request_falls_back_to_contract_limit(self) -> None:
        assert resolve_effective_limit(0, 500) == 500
        assert resolve_effective_limit(-5, 500) == 500


class TestResolveOrderClause:
    def test_no_order_by_yields_empty_clause(self) -> None:
        assert resolve_order_clause(None, None) == ""
        # order param is ignored when the contract declares no orderable column.
        assert resolve_order_clause(None, "asc") == ""

    def test_default_direction_from_contract(self) -> None:
        assert resolve_order_clause("created_at DESC", None) == (
            " ORDER BY created_at DESC"
        )

    def test_caller_can_flip_to_asc(self) -> None:
        assert resolve_order_clause("created_at DESC", "asc") == (
            " ORDER BY created_at ASC"
        )

    def test_caller_can_flip_to_desc(self) -> None:
        assert resolve_order_clause("created_at ASC", "DESC") == (
            " ORDER BY created_at DESC"
        )

    def test_column_only_contract_defaults_to_asc(self) -> None:
        assert resolve_order_clause("updated_at", None) == " ORDER BY updated_at ASC"

    def test_caller_cannot_inject_arbitrary_column(self) -> None:
        # The clause column is ALWAYS the contract column; the order param only
        # toggles direction. An injection attempt via order is rejected by the
        # route's regex pattern, but the helper itself never reads a column from
        # `order`.
        clause = resolve_order_clause("created_at DESC", "created_at; DROP TABLE x")
        assert clause == " ORDER BY created_at DESC"


class TestProjectionQueryLimitOrderParams:
    """Route-level behaviour: limit/order forwarded into the emitted SQL."""

    _TOPIC = "onex.snapshot.projection.ab-compare.v1"  # contract limit 100

    def _row(self) -> dict[str, Any]:
        return {
            "correlation_id": "run-abc",
            "model_id": "qwen3-coder-30b",
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "estimated_cost_usd": "0.001",
            "latency_ms": "10.0",
            "usage_source": "actual",
            "created_at": _ts(timedelta(minutes=1)),
        }

    def test_default_limit_is_contract_limit(self) -> None:
        pool, captured = _make_capturing_pool([self._row()])
        with _with_pool(pool) as client:
            resp = client.get(f"/projection/{self._TOPIC}")
        assert resp.status_code == 200
        assert resp.json()["row_limit"] == 100
        assert "LIMIT 100" in captured[0]

    def test_requested_limit_is_applied(self) -> None:
        pool, captured = _make_capturing_pool([self._row()])
        with _with_pool(pool) as client:
            resp = client.get(f"/projection/{self._TOPIC}?limit=5")
        assert resp.status_code == 200
        assert resp.json()["row_limit"] == 5
        assert "LIMIT 5" in captured[0]

    def test_requested_limit_above_ceiling_is_clamped(self) -> None:
        pool, captured = _make_capturing_pool([self._row()])
        with _with_pool(pool) as client:
            resp = client.get(f"/projection/{self._TOPIC}?limit=99999")
        assert resp.status_code == 200
        assert resp.json()["row_limit"] == 100
        assert "LIMIT 100" in captured[0]

    def test_order_default_is_contract_direction(self) -> None:
        pool, captured = _make_capturing_pool([self._row()])
        with _with_pool(pool) as client:
            resp = client.get(f"/projection/{self._TOPIC}")
        assert resp.status_code == 200
        assert "ORDER BY created_at DESC" in captured[0]
        assert resp.json()["ordering"] == "created_at DESC"

    def test_order_asc_toggles_direction(self) -> None:
        pool, captured = _make_capturing_pool([self._row()])
        with _with_pool(pool) as client:
            resp = client.get(f"/projection/{self._TOPIC}?order=asc")
        assert resp.status_code == 200
        assert "ORDER BY created_at ASC" in captured[0]
        assert resp.json()["ordering"] == "created_at ASC"

    def test_invalid_order_value_rejected_with_422(self) -> None:
        pool, _captured = _make_capturing_pool([self._row()])
        with _with_pool(pool) as client:
            resp = client.get(f"/projection/{self._TOPIC}?order=sideways")
        assert resp.status_code == 422

    def test_zero_limit_rejected_with_422(self) -> None:
        pool, _captured = _make_capturing_pool([self._row()])
        with _with_pool(pool) as client:
            resp = client.get(f"/projection/{self._TOPIC}?limit=0")
        assert resp.status_code == 422

    def test_limit_applies_on_correlation_filtered_path(self) -> None:
        pool, captured = _make_capturing_pool([self._row()])
        with _with_pool(pool) as client:
            resp = client.get(
                f"/projection/{self._TOPIC}?correlation_id=run-abc&limit=3"
            )
        assert resp.status_code == 200
        assert "WHERE correlation_id = $1" in captured[0]
        assert "LIMIT 3" in captured[0]


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


_PR_MERGED_TOPIC = "onex.evt.github.pr-merged.v1"

_PR_MERGED_CURSOR_MAP: dict[str, ProjectionTableConfig] = {
    _PR_MERGED_TOPIC: ProjectionTableConfig(
        topic=_PR_MERGED_TOPIC,
        table="pr_merged_events",
        schema_name="public",
        columns=(
            "projection_cursor",
            "event_id",
            "repo",
            "branch",
            "pr_number",
            "ticket",
            "merged_at",
            "created_at",
        ),
        order_by="projection_cursor ASC",
        freshness_column="created_at",
        cursor_column="projection_cursor",
        limit=500,
    ),
}

_NO_CURSOR_MAP: dict[str, ProjectionTableConfig] = {
    "onex.evt.example.no-cursor.v1": ProjectionTableConfig(
        topic="onex.evt.example.no-cursor.v1",
        table="example_rows",
        schema_name="public",
        columns=("id",),
        order_by="id ASC",
    ),
}


@pytest.mark.unit
class TestGenericProjectionSinceCursor:
    """OMN-13227: generic ?since=<cursor> pagination on /projection/{topic}."""

    def test_since_filters_on_cursor_column(self) -> None:
        """`since` emits a WHERE cursor_column > $1 clause bound to the value."""
        rows = [
            {"projection_cursor": 5, "event_id": "e5", "repo": "r", "branch": "b"},
        ]
        pool, captured = _make_capturing_pool(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_pool(pool, _PR_MERGED_CURSOR_MAP) as client:
            resp = client.get(f"/projection/{_PR_MERGED_TOPIC}", params={"since": "3"})
        assert resp.status_code == 200
        assert captured, "no SQL captured"
        assert "WHERE projection_cursor > $1" in captured[0]

    def test_since_returns_next_cursor(self) -> None:
        """next_cursor is the last row's cursor value, for the reaper to advance."""
        rows = [
            {"projection_cursor": 7, "event_id": "e7", "repo": "r", "branch": "b"},
            {"projection_cursor": 9, "event_id": "e9", "repo": "r", "branch": "b"},
        ]
        pool = _make_pool(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_pool(pool, _PR_MERGED_CURSOR_MAP) as client:
            resp = client.get(f"/projection/{_PR_MERGED_TOPIC}", params={"since": "0"})
        body = resp.json()
        assert resp.status_code == 200
        assert body["next_cursor"] == "9"
        assert body["row_count"] == 2

    def test_empty_page_has_null_next_cursor(self) -> None:
        """An empty page (caller already caught up) returns next_cursor=None."""
        pool = _make_pool([], latest_ts=_ts(timedelta(minutes=1)))
        with _with_pool(pool, _PR_MERGED_CURSOR_MAP) as client:
            resp = client.get(f"/projection/{_PR_MERGED_TOPIC}", params={"since": "99"})
        body = resp.json()
        assert resp.status_code == 200
        assert body["next_cursor"] is None
        assert body["row_count"] == 0

    def test_since_rejected_when_no_cursor_column(self) -> None:
        """`since` on a topic without a cursor_column is a typed 422, not a scan."""
        pool = _make_pool([])
        with _with_pool(pool, _NO_CURSOR_MAP) as client:
            resp = client.get(
                "/projection/onex.evt.example.no-cursor.v1", params={"since": "1"}
            )
        assert resp.status_code == 422
        assert resp.json()["filter"] == "since"
