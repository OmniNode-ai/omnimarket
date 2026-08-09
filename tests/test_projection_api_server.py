"""Tests for projection_api_server (OMN-10461 / OMN-10490 / OMN-15800).

OMN-15800 (2026-08-09 operator ruling): this process holds ZERO database
driver. Every route is served from an in-memory SnapshotCache. This file
replaces the prior asyncpg-pool-backed test suite -- the DB-serving-specific
tests (DSN resolution, resolve_order_clause, SQL-string assertions) are
deleted because the functionality they tested was deleted, not simplified.

Covers:
- Unknown topic → 404 with available_topics list
- A topic not yet flipped bus_backed → 503 not_yet_bus_backed
- Response envelope shape for a bus_backed topic
- Freshness computation (fresh / stale / degraded / unknown)
- correlation_id filter parameter is forwarded / rejected per column declaration
- An unbootstrapped SnapshotCache → 503 snapshot_bootstrap_incomplete
- /health is bus-cache liveness; /ready fails closed on incomplete bootstrap
- /projections returns full metadata per topic
- generic since/cursor pagination is served from the in-memory cache
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from omnimarket.projection.models import ProjectionTableConfig
from scripts.projection_api_server import (
    _cors_origins_from_env,
    app,
    compute_freshness,
    get_snapshot_cache,
    get_topic_map,
    resolve_effective_limit,
)

# ---------------------------------------------------------------------------
# Canonical topic map matching the contracts exposed through projection_api.
# Only the 2 OMN-15800 slice families declare bus_backed=True for real; the
# rest of this fixture map is bus_backed=True purely so these generic
# envelope/freshness tests can exercise the serving path -- it does not
# assert anything about which real production topics have converted.
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
        order_by_spec=(("created_at", "DESC"),),
        freshness_column="created_at",
        limit=100,
        source_contract="ab_compare_reducer",
        bus_backed=True,
        key_columns=("correlation_id",),
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
        order_by_spec=(("updated_at", "DESC"),),
        freshness_column="updated_at",
        limit=100,
        source_contract="node_projection_cost_summary",
        bus_backed=True,
        key_columns=("aggregation_key",),
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
        order_by_spec=(("created_at", "DESC"),),
        freshness_column="created_at",
        limit=100,
        source_contract="node_projection_cost_token_usage",
        bus_backed=True,
        key_columns=("model_id", "created_at"),
    ),
    "onex.snapshot.projection.registration.v1": ProjectionTableConfig(
        topic="onex.snapshot.projection.registration.v1",
        table="node_service_registry",
        schema_name="public",
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
        order_by_spec=(("updated_at", "DESC"),),
        freshness_column="updated_at",
        limit=100,
        source_contract="projection_registration",
        bus_backed=True,
        key_columns=("service_name",),
    ),
}

_SYSTEM_EVENTS_TOPIC = "onex.snapshot.projection.live-events.v1"
_SYSTEM_EVENTS_CONFIG = ProjectionTableConfig(
    topic=_SYSTEM_EVENTS_TOPIC,
    table="live_events",
    columns=("event_id", "correlation_id", "topic", "type", "summary"),
    order_by="created_at DESC",
    order_by_spec=(("created_at", "DESC"),),
    freshness_column="created_at",
    source_contract="node_projection_live_events",
    bus_backed=True,
    key_columns=("event_id",),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat()


def _make_cache(
    rows_by_topic: dict[str, list[dict[str, Any]]] | list[dict[str, Any]],
    latest_ts: str | None = None,
    *,
    bootstrapped: bool = True,
) -> MagicMock:
    """Fake SnapshotCache. Accepts either a single row list (applied to every
    topic queried) or a per-topic dict for multi-topic tests."""
    cache = MagicMock()
    cache.is_bootstrapped = MagicMock(return_value=bootstrapped)

    if isinstance(rows_by_topic, dict):
        cache.get_rows = MagicMock(
            side_effect=lambda topic, **_kwargs: rows_by_topic.get(topic, [])
        )
    else:
        cache.get_rows = MagicMock(return_value=rows_by_topic)

    parsed_latest = (
        datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
        if latest_ts is not None
        else None
    )
    cache.latest_event_at = MagicMock(return_value=parsed_latest)
    return cache


@contextmanager
def _with_cache(
    cache: MagicMock,
    topic_map: dict[str, ProjectionTableConfig] | None = None,
) -> Generator[TestClient, None, None]:
    """Override get_snapshot_cache (and optionally get_topic_map); yield a TestClient."""
    effective_map = topic_map if topic_map is not None else _PROJECTION_TOPIC_MAP
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    app.dependency_overrides[get_topic_map] = lambda: effective_map
    client = TestClient(app, raise_server_exceptions=True)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def _assert_envelope(body: dict[str, Any], topic: str) -> None:
    assert body["topic"] == topic
    assert "projection_version" in body
    assert "generated_at" in body
    assert "data_freshness" in body
    assert body["data_freshness"] in {"fresh", "idle", "stale", "degraded", "unknown"}
    assert "row_count" in body
    assert "rows" in body
    assert isinstance(body["rows"], list)
    assert body["backing"] == "bus"


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


class TestComputeFreshness:
    """Freshness classification, including contract-cadence + idle (OMN-13035)."""

    def test_none_returns_degraded(self) -> None:
        assert compute_freshness(None) == "degraded"
        assert compute_freshness(None, expected_event_interval_seconds=60) == "degraded"

    def test_on_demand_recent_is_fresh(self) -> None:
        assert compute_freshness(_ts(timedelta(minutes=2))) == "fresh"

    def test_on_demand_quiet_is_idle_not_stale(self) -> None:
        assert compute_freshness(_ts(timedelta(minutes=30))) == "idle"

    def test_on_demand_long_silence_is_still_idle_not_degraded(self) -> None:
        result = compute_freshness(_ts(timedelta(hours=6)))
        assert result == "idle"
        assert result not in {"stale", "degraded"}

    def test_cadenced_within_interval_is_fresh(self) -> None:
        assert (
            compute_freshness(
                _ts(timedelta(seconds=30)), expected_event_interval_seconds=60
            )
            == "fresh"
        )

    def test_cadenced_one_missed_beat_is_idle(self) -> None:
        assert (
            compute_freshness(
                _ts(timedelta(seconds=90)), expected_event_interval_seconds=60
            )
            == "idle"
        )

    def test_cadenced_behind_two_intervals_is_stale(self) -> None:
        assert (
            compute_freshness(
                _ts(timedelta(seconds=180)), expected_event_interval_seconds=60
            )
            == "stale"
        )


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
            assert cfg.order_by is not None, f"{topic} missing order_by"

    def test_all_topics_have_freshness_column(self) -> None:
        for topic, cfg in _PROJECTION_TOPIC_MAP.items():
            assert cfg.freshness_column is not None, f"{topic} missing freshness_column"


class TestProjectionRoutes:
    def test_unknown_topic_returns_404(self) -> None:
        cache = _make_cache([])
        with _with_cache(cache) as client:
            resp = client.get("/projection/onex.snapshot.projection.does.not.exist.v1")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "unknown_topic"
        assert set(body["available_topics"]) == set(_PROJECTION_TOPIC_MAP.keys())

    def test_not_yet_bus_backed_topic_returns_503(self) -> None:
        topic = "onex.snapshot.projection.not-converted.v1"
        cfg = ProjectionTableConfig(
            topic=topic,
            table="some_table",
            columns=("id",),
            source_contract="node_test",
        )
        cache = _make_cache([])
        with _with_cache(cache, {topic: cfg}) as client:
            resp = client.get(f"/projection/{topic}")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "not_yet_bus_backed"
        assert body["migration_ticket"] == "OMN-15800"
        cache.get_rows.assert_not_called()

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
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.status_code == 200
        _assert_envelope(resp.json(), "onex.snapshot.projection.cost.summary.v1")
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
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=10)))
        with _with_cache(cache) as client:
            resp = client.get("/projection/onex.snapshot.projection.registration.v1")
        assert resp.status_code == 200
        _assert_envelope(resp.json(), "onex.snapshot.projection.registration.v1")
        assert resp.json()["row_count"] == 1

    def test_freshness_fresh(self) -> None:
        cache = _make_cache([], latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.json()["data_freshness"] == "fresh"

    def test_on_demand_quiet_reports_idle_not_stale(self) -> None:
        cache = _make_cache([], latest_ts=_ts(timedelta(minutes=30)))
        with _with_cache(cache) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.json()["data_freshness"] == "idle"

    def test_cadenced_topic_behind_cadence_reports_stale(self) -> None:
        topic = "onex.snapshot.projection.cost.summary.v1"
        cadenced = _PROJECTION_TOPIC_MAP[topic].model_copy(
            update={"expected_event_interval_seconds": 60}
        )
        cache = _make_cache([], latest_ts=_ts(timedelta(minutes=30)))
        with _with_cache(cache, {topic: cadenced}) as client:
            resp = client.get(f"/projection/{topic}")
        assert resp.json()["data_freshness"] == "stale"

    def test_unbootstrapped_cache_returns_503(self) -> None:
        cache = _make_cache([], bootstrapped=False)
        with _with_cache(cache) as client:
            resp = client.get("/projection/onex.snapshot.projection.cost.summary.v1")
        assert resp.status_code == 503
        assert resp.json()["error"] == "snapshot_bootstrap_incomplete"

    def test_correlation_id_filter_rejected_for_aggregate_topic(self) -> None:
        """Aggregate topics without correlation_id expose typed 422 before the
        cache is even queried."""
        cache = _make_cache([])
        with _with_cache(cache) as client:
            resp = client.get(
                "/projection/onex.snapshot.projection.cost.summary.v1",
                params={"correlation_id": "corr-abc"},
            )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "unsupported_filter"
        assert body["filter"] == "correlation_id"
        cache.get_rows.assert_not_called()

    def test_ab_compare_correlation_id_filter_returns_matching_row(self) -> None:
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
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=2)))
        with _with_cache(cache) as client:
            resp = client.get(
                "/projection/onex.snapshot.projection.ab-compare.v1",
                params={"correlation_id": "run-abc"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 1
        assert body["rows"][0]["correlation_id"] == "run-abc"


class TestHealthRoute:
    def test_health_returns_ok_with_bus_backed_topics(self) -> None:
        cache = _make_cache([])
        cache.bus_backed_topics = frozenset(_PROJECTION_TOPIC_MAP.keys())
        with _with_cache(cache) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert set(body["bus_backed_topics"]) == set(_PROJECTION_TOPIC_MAP.keys())


class TestReadyRoute:
    def test_ready_when_every_bus_backed_topic_bootstrapped(self) -> None:
        cache = _make_cache([], bootstrapped=True)
        with _with_cache(cache) as client:
            resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_not_ready_when_a_topic_has_not_bootstrapped(self) -> None:
        cache = _make_cache([], bootstrapped=False)
        with _with_cache(cache) as client:
            resp = client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not_ready"

    def test_not_ready_when_no_topic_is_bus_backed(self) -> None:
        topic = "onex.snapshot.projection.not-converted.v1"
        cfg = ProjectionTableConfig(
            topic=topic, table="t", columns=("id",), source_contract="node_test"
        )
        cache = _make_cache([], bootstrapped=True)
        with _with_cache(cache, {topic: cfg}) as client:
            resp = client.get("/ready")
        assert resp.status_code == 503


class TestProjectionsListRoute:
    def test_projections_returns_metadata_for_all_topics(self) -> None:
        cache = _make_cache([])
        with _with_cache(cache) as client:
            resp = client.get("/projections")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["topics"]) == 4
        topic_names = {t["topic"] for t in body["topics"]}
        assert topic_names == set(_PROJECTION_TOPIC_MAP.keys())

    def test_projections_entry_has_required_fields(self) -> None:
        cache = _make_cache([])
        with _with_cache(cache) as client:
            resp = client.get("/projections")
        body = resp.json()
        for entry in body["topics"]:
            assert "topic" in entry
            assert "table" in entry
            assert "status" in entry
            assert "columns" in entry
            assert "limit" in entry
            assert "source_contract" in entry
            assert "bus_backed" in entry
            assert "backing" in entry


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


class TestProjectionQueryLimitOrderParams:
    """Route-level behaviour: limit/order reflected in the response envelope."""

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
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}")
        assert resp.status_code == 200
        assert resp.json()["row_limit"] == 100

    def test_requested_limit_is_applied(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}?limit=5")
        assert resp.status_code == 200
        assert resp.json()["row_limit"] == 5

    def test_requested_limit_above_ceiling_is_clamped(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}?limit=99999")
        assert resp.status_code == 200
        assert resp.json()["row_limit"] == 100

    def test_order_default_is_contract_direction(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}")
        assert resp.status_code == 200
        assert resp.json()["ordering"] == "created_at DESC"

    def test_order_asc_toggles_direction(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}?order=asc")
        assert resp.status_code == 200
        assert resp.json()["ordering"] == "created_at ASC"

    def test_invalid_order_value_rejected_with_422(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}?order=sideways")
        assert resp.status_code == 422

    def test_zero_limit_rejected_with_422(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}?limit=0")
        assert resp.status_code == 422

    def test_limit_applies_on_correlation_filtered_path(self) -> None:
        rows = [self._row(), {**self._row(), "correlation_id": "other"}]
        cache = _make_cache(rows)
        with _with_cache(cache) as client:
            resp = client.get(
                f"/projection/{self._TOPIC}?correlation_id=run-abc&limit=3"
            )
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 1


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
        order_by_spec=(("projection_cursor", "ASC"),),
        freshness_column="created_at",
        cursor_column="projection_cursor",
        limit=500,
        bus_backed=True,
        key_columns=("projection_cursor",),
    ),
}

_NO_CURSOR_MAP: dict[str, ProjectionTableConfig] = {
    "onex.evt.example.no-cursor.v1": ProjectionTableConfig(
        topic="onex.evt.example.no-cursor.v1",
        table="example_rows",
        schema_name="public",
        columns=("id",),
        order_by="id ASC",
        order_by_spec=(("id", "ASC"),),
        bus_backed=True,
        key_columns=("id",),
    ),
}


@pytest.mark.unit
class TestGenericProjectionSinceCursor:
    """OMN-13227: generic ?since=<cursor> pagination on /projection/{topic},
    now served from the in-memory cache's already-materialized rows."""

    def test_since_filters_on_cursor_column(self) -> None:
        rows = [
            {"projection_cursor": "3", "event_id": "e3", "repo": "r", "branch": "b"},
            {"projection_cursor": "5", "event_id": "e5", "repo": "r", "branch": "b"},
        ]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, _PR_MERGED_CURSOR_MAP) as client:
            resp = client.get(f"/projection/{_PR_MERGED_TOPIC}", params={"since": "3"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 1
        assert body["rows"][0]["projection_cursor"] == "5"

    def test_since_returns_next_cursor(self) -> None:
        rows = [
            {"projection_cursor": "7", "event_id": "e7", "repo": "r", "branch": "b"},
            {"projection_cursor": "9", "event_id": "e9", "repo": "r", "branch": "b"},
        ]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, _PR_MERGED_CURSOR_MAP) as client:
            resp = client.get(f"/projection/{_PR_MERGED_TOPIC}", params={"since": "0"})
        body = resp.json()
        assert resp.status_code == 200
        assert body["next_cursor"] == "9"
        assert body["row_count"] == 2

    def test_empty_page_has_null_next_cursor(self) -> None:
        cache = _make_cache([], latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, _PR_MERGED_CURSOR_MAP) as client:
            resp = client.get(f"/projection/{_PR_MERGED_TOPIC}", params={"since": "99"})
        body = resp.json()
        assert resp.status_code == 200
        assert body["next_cursor"] is None
        assert body["row_count"] == 0

    def test_since_rejected_when_no_cursor_column(self) -> None:
        cache = _make_cache([])
        with _with_cache(cache, _NO_CURSOR_MAP) as client:
            resp = client.get(
                "/projection/onex.evt.example.no-cursor.v1", params={"since": "1"}
            )
        assert resp.status_code == 422
        assert resp.json()["filter"] == "since"
