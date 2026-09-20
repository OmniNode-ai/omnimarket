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
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from omnimarket.projection.discovery import load_projection_exposures_from_contract
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
        order_by_spec=(("created_at", "DESC", None),),
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
        order_by_spec=(("updated_at", "DESC", None),),
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
        order_by_spec=(("created_at", "DESC", None),),
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
        order_by_spec=(("updated_at", "DESC", None),),
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
    order_by_spec=(("created_at", "DESC", None),),
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
    # OMN-15876: /ready now also reads the consumer's liveness and per-topic
    # partition assignment. A bare MagicMock would answer a truthy sentinel
    # for consume_failure and a non-serializable one for the partition count,
    # so the default fake states the HEALTHY shape explicitly; tests that
    # exercise the failure direction override these.
    cache.consume_failure = None
    cache.assigned_partition_count = MagicMock(return_value=1)
    # OMN-18905: the double must state the CAUGHT-UP shape explicitly, the
    # same reason `consume_failure` above does. A bare MagicMock answers a
    # truthy sentinel for `is_stale`, which would make every fixture in this
    # file assert against a response that says its own rows are frozen --
    # and, worse, would let a real staleness regression pass here unnoticed.
    # Tests exercising the stale direction override these two.
    cache.is_stale = MagicMock(return_value=False)
    cache.lag_report = MagicMock(
        return_value={
            "applied_offset": 0,
            "end_offset": 0,
            "lag": 0,
            "partitions": 1,
        }
    )

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

    def test_not_ready_when_the_consume_loop_died_even_if_all_bootstrapped(
        self,
    ) -> None:
        """OMN-15876, the fail-OPEN half.

        Every topic finished its INITIAL replay, then the consume task died.
        Pre-fix this answered 200 while serving a cache that had silently
        stopped updating -- a frozen read model rendered as live state. A
        dead consumer must refuse readiness on its own.
        """
        cache = _make_cache([], bootstrapped=True)
        cache.consume_failure = "IllegalStateError: Partition ... is not assigned"
        with _with_cache(cache) as client:
            resp = client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        assert body["consumer_failure"] == (
            "IllegalStateError: Partition ... is not assigned"
        )

    def test_ready_body_names_the_partition_assignment_per_topic(self) -> None:
        """OMN-15876, the discriminator.

        ``bootstrapped=False`` renders identically for "assigned, still
        replaying" and "never assigned a partition, so this can NEVER become
        True". On a broker with auto-create off the second means the topic
        does not exist. Five consecutive staging rollouts failed on a body
        that could not tell them apart.
        """
        cache = _make_cache([], bootstrapped=False)
        cache.assigned_partition_count = MagicMock(return_value=0)
        with _with_cache(cache) as client:
            resp = client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["assigned_partitions"], "no per-topic assignment map in the body"
        assert all(count == 0 for count in body["assigned_partitions"].values())
        assert set(body["assigned_partitions"]) == set(body["bus_backed_topics"])

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
        # Regression guard (CodeRabbit, OMN-15800): the requested direction
        # must reach the ACTUAL row order via get_rows(order_by_override=...),
        # not just the reported "ordering" string.
        _args, kwargs = cache.get_rows.call_args
        assert kwargs["order_by_override"] == (("created_at", "ASC", None),)

    def test_order_default_reaches_cache_as_contract_direction(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}")
        assert resp.status_code == 200
        _args, kwargs = cache.get_rows.call_args
        assert kwargs["order_by_override"] == (("created_at", "DESC", None),)

    def test_invalid_order_value_rejected_with_422(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}?order=sideways")
        assert resp.status_code == 422

    def test_order_by_column_reaches_cache_as_override(self) -> None:
        """OMN-16290: a requested ``order_by`` column must reach the ACTUAL
        row order via get_rows(order_by_override=...), replacing the
        contract default entirely -- not merely direction-flip it."""
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}?order_by=model_id")
        assert resp.status_code == 200
        assert resp.json()["ordering"] == "model_id ASC"
        _args, kwargs = cache.get_rows.call_args
        assert kwargs["order_by_override"] == (("model_id", "ASC", None),)

    def test_order_by_with_explicit_direction(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}?order_by=model_id+DESC")
        assert resp.status_code == 200
        assert resp.json()["ordering"] == "model_id DESC"
        _args, kwargs = cache.get_rows.call_args
        assert kwargs["order_by_override"] == (("model_id", "DESC", None),)

    def test_order_by_composes_with_order_direction_flip(self) -> None:
        """``order_by`` selects the column; ``order`` still flips ITS
        direction (composability, not a competing/ignored knob)."""
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(
                f"/projection/{self._TOPIC}?order_by=model_id+ASC&order=desc"
            )
        assert resp.status_code == 200
        assert resp.json()["ordering"] == "model_id DESC"
        _args, kwargs = cache.get_rows.call_args
        assert kwargs["order_by_override"] == (("model_id", "DESC", None),)

    def test_order_by_unknown_column_rejected_with_422_not_silently_ignored(
        self,
    ) -> None:
        """no-defensive-defaults: an order_by naming a column this topic does
        not declare is REJECTED, never silently dropped back to the contract
        default ordering."""
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}?order_by=not_a_column")
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "invalid_order_by"
        assert body["topic"] == self._TOPIC
        # The contract default must never have been reached for row order.
        cache.get_rows.assert_not_called()

    def test_order_by_malformed_clause_rejected_with_422(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(
                f"/projection/{self._TOPIC}?order_by=model_id+GARBAGE+TOKENS"
            )
        assert resp.status_code == 422
        assert resp.json()["error"] == "invalid_order_by"

    def test_absent_order_by_preserves_contract_default(self) -> None:
        cache = _make_cache([self._row()])
        with _with_cache(cache) as client:
            resp = client.get(f"/projection/{self._TOPIC}")
        assert resp.status_code == 200
        assert resp.json()["ordering"] == "created_at DESC"
        _args, kwargs = cache.get_rows.call_args
        assert kwargs["order_by_override"] == (("created_at", "DESC", None),)

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
        order_by_spec=(("projection_cursor", "ASC", None),),
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
        order_by_spec=(("id", "ASC", None),),
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
        # OMN-17215: a third row and a limit of two make this page genuinely
        # truncated. Both assertions below are unchanged; only the fixture moved,
        # because a cursor is now owed on truncation rather than on non-emptiness.
        rows = [
            {"projection_cursor": "7", "event_id": "e7", "repo": "r", "branch": "b"},
            {"projection_cursor": "9", "event_id": "e9", "repo": "r", "branch": "b"},
            {"projection_cursor": "11", "event_id": "e11", "repo": "r", "branch": "b"},
        ]
        cfg = _PR_MERGED_CURSOR_MAP[_PR_MERGED_TOPIC]
        truncating = {_PR_MERGED_TOPIC: cfg.model_copy(update={"limit": 2})}
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, truncating) as client:
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

    def test_cursor_walk_is_ascending_even_when_presentation_is_descending(
        self,
    ) -> None:
        """OMN-18043: page in cursor order, then sort only the returned page."""
        topic = _PR_MERGED_TOPIC
        cfg = _PR_MERGED_CURSOR_MAP[topic].model_copy(
            update={
                "order_by": "projection_cursor DESC",
                "order_by_spec": (("projection_cursor", "DESC", None),),
                "limit": 2,
            }
        )
        rows = [
            {"projection_cursor": "1", "event_id": "e1"},
            {"projection_cursor": "2", "event_id": "e2"},
            {"projection_cursor": "3", "event_id": "e3"},
        ]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, {topic: cfg}) as client:
            resp = client.get(f"/projection/{topic}", params={"since": "0"})
        assert resp.status_code == 200
        assert [row["projection_cursor"] for row in resp.json()["rows"]] == [
            "2",
            "1",
        ]
        assert resp.json()["next_cursor"] == "2"
        kwargs = cache.get_rows.call_args.kwargs
        assert kwargs["order_by_override"] == (("projection_cursor", "ASC", None),)


@pytest.mark.unit
class TestNextCursorSignalsTruncation:
    """OMN-17215 AC3: ``next_cursor`` must distinguish "this is the complete
    set" from "this is page 1 of N".

    The condition it is guarded by tests whether the page is *non-empty*, not
    whether it is *truncated*, so a complete result that happens to have rows
    still advertises a cursor. A client that follows it fetches an empty page
    and cannot tell "done" from "more".
    """

    @staticmethod
    def _map_with_limit(limit: int) -> dict[str, ProjectionTableConfig]:
        cfg = _PR_MERGED_CURSOR_MAP[_PR_MERGED_TOPIC]
        return {
            _PR_MERGED_TOPIC: cfg.model_copy(update={"limit": limit}),
        }

    def test_complete_page_has_null_next_cursor(self) -> None:
        """Two rows under a limit of 500 is the whole set — nothing follows it."""
        rows = [
            {"projection_cursor": "7", "event_id": "e7", "repo": "r", "branch": "b"},
            {"projection_cursor": "9", "event_id": "e9", "repo": "r", "branch": "b"},
        ]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, _PR_MERGED_CURSOR_MAP) as client:
            resp = client.get(f"/projection/{_PR_MERGED_TOPIC}", params={"since": "0"})
        body = resp.json()
        assert resp.status_code == 200
        assert body["row_count"] == 2
        assert body["row_count"] < body["row_limit"]
        assert body["next_cursor"] is None

    def test_truncated_page_still_returns_next_cursor(self) -> None:
        """Positive control: the fix must not null the cursor unconditionally.

        Three rows under a limit of two IS truncated, so the cursor is owed and
        must name the last row actually served.
        """
        rows = [
            {"projection_cursor": "1", "event_id": "e1", "repo": "r", "branch": "b"},
            {"projection_cursor": "2", "event_id": "e2", "repo": "r", "branch": "b"},
            {"projection_cursor": "3", "event_id": "e3", "repo": "r", "branch": "b"},
        ]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, self._map_with_limit(2)) as client:
            resp = client.get(f"/projection/{_PR_MERGED_TOPIC}", params={"since": "0"})
        body = resp.json()
        assert resp.status_code == 200
        assert body["row_count"] == 2
        assert body["row_count"] == body["row_limit"]
        assert body["next_cursor"] == "2"


# ---------------------------------------------------------------------------
# OMN-17215 AC4: the consumer-flow default page is ranked, not lexical
# ---------------------------------------------------------------------------

_CONSUMER_FLOW_TOPIC = "onex.snapshot.projection.consumer-flow.v1"
_CONSUMER_FLOW_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_consumer_flow"
    / "contract.yaml"
)


def _consumer_flow_cfg(**update: Any) -> ProjectionTableConfig:
    """The REAL consumer-flow exposure, parsed from its contract.yaml.

    Loading the shipped contract (rather than hand-building a config) is the
    point: the rank under test is the one the contract declares, so deleting or
    loosening that declaration turns these tests red.
    """
    contract = yaml.safe_load(_CONSUMER_FLOW_CONTRACT_PATH.read_text())
    (cfg,) = load_projection_exposures_from_contract(
        contract, "projection_consumer_flow", _CONSUMER_FLOW_CONTRACT_PATH
    )
    assert cfg.topic == _CONSUMER_FLOW_TOPIC
    return cfg.model_copy(update=update) if update else cfg


def _flow_row(cursor: int, flow_state: str, window_end_second: int) -> dict[str, Any]:
    return {
        "projection_cursor": cursor,
        "consumer_group": f"group-{cursor}",
        "topic": f"topic-{cursor}",
        "window_end": f"2026-09-15T00:{window_end_second // 60:02d}:"
        f"{window_end_second % 60:02d}+00:00",
        "flow_state": flow_state,
    }


@pytest.mark.unit
class TestConsumerFlowRankedPresentation:
    """OMN-17215 AC4: STALLED, STARVED, and UNKNOWN are presented first, then
    FLOWING, then IDLE, from the rank the contract declares, then
    ``window_end DESC, projection_cursor DESC`` inside each tier.

    Falsified by a STALLED row sitting behind FLOWING or IDLE rows on a served
    page.
    """

    def test_non_idle_rows_lead_the_page_then_window_end_then_cursor(self) -> None:
        # Supplied in ascending cursor order, as the cache returns a page
        # selection. Every non-IDLE row is OLDER than every IDLE row, so the
        # pre-AC4 ``window_end DESC`` order would bury all of them.
        rows = [
            _flow_row(1, "IDLE", 10),
            _flow_row(2, "IDLE", 10),
            _flow_row(3, "IDLE", 9),
            _flow_row(4, "IDLE", 8),
            _flow_row(5, "FLOWING", 3),
            _flow_row(6, "IDLE", 7),
            _flow_row(7, "STALLED", 2),
            _flow_row(8, "IDLE", 6),
            _flow_row(9, "STARVED", 1),
            _flow_row(10, "UNKNOWN", 2),
        ]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, {_CONSUMER_FLOW_TOPIC: _consumer_flow_cfg()}) as client:
            resp = client.get(f"/projection/{_CONSUMER_FLOW_TOPIC}")
        assert resp.status_code == 200
        body = resp.json()
        assert [row["projection_cursor"] for row in body["rows"]] == [
            10,  # UNKNOWN window_end 2, cursor 10 beats 7 on the tie
            7,  # STALLED  window_end 2
            9,  # STARVED  window_end 1
            5,  # FLOWING  window_end 3, own tier below the attention states
            2,  # IDLE     window_end 10, cursor 2 beats 1 on the tie
            1,
            3,
            4,
            6,
            8,
        ]
        assert body["ordering"] == (
            "CASE WHEN flow_state IN ('STALLED', 'STARVED', 'UNKNOWN') THEN 0 "
            "WHEN flow_state IN ('FLOWING') THEN 1 "
            "WHEN flow_state IN ('IDLE') THEN 2 END ASC, "
            "window_end DESC, projection_cursor DESC"
        )

    def test_stalled_with_older_window_sorts_above_flowing_with_later_window(
        self,
    ) -> None:
        """OMN-17215 AC4 follow-up: FLOWING needs no attention, so a STALLED
        group is never pushed below FLOWING groups whose windows are later."""
        rows = [
            _flow_row(1, "FLOWING", 50),
            _flow_row(2, "STALLED", 5),
            _flow_row(3, "FLOWING", 40),
            _flow_row(4, "IDLE", 60),
        ]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, {_CONSUMER_FLOW_TOPIC: _consumer_flow_cfg()}) as client:
            resp = client.get(f"/projection/{_CONSUMER_FLOW_TOPIC}")
        assert resp.status_code == 200
        assert [
            (row["projection_cursor"], row["flow_state"]) for row in resp.json()["rows"]
        ] == [
            (2, "STALLED"),  # window_end 5, older than both FLOWING rows
            (1, "FLOWING"),  # window_end 50
            (3, "FLOWING"),  # window_end 40
            (4, "IDLE"),  # window_end 60, latest of all
        ]

    def test_live_shaped_page_serves_every_non_idle_row_first(self) -> None:
        """The 2026-09-15 live readback: 494 IDLE rows and six non-IDLE rows at
        window_end-DESC positions 102/155/338/378/398/414 of a 500-row page."""
        late_positions = {102, 155, 338, 378, 398, 414}
        non_idle = iter(["STALLED", "STARVED", "UNKNOWN"] * 2)
        # Position p (1-based) in window_end DESC order has window_end 1000 - p.
        rows = sorted(
            (
                _flow_row(
                    cursor=p,
                    flow_state=next(non_idle) if p in late_positions else "IDLE",
                    window_end_second=1000 - p,
                )
                for p in range(1, 501)
            ),
            key=lambda row: row["projection_cursor"],
        )
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, {_CONSUMER_FLOW_TOPIC: _consumer_flow_cfg()}) as client:
            resp = client.get(f"/projection/{_CONSUMER_FLOW_TOPIC}")
        assert resp.status_code == 200
        served = resp.json()["rows"]
        assert len(served) == 500
        assert [row["projection_cursor"] for row in served[:6]] == sorted(
            late_positions
        )
        assert all(row["flow_state"] != "IDLE" for row in served[:6])
        assert all(row["flow_state"] == "IDLE" for row in served[6:])

    def test_cursor_walk_is_unchanged_by_the_rank(self) -> None:
        """OMN-18043 ruling holds: pages are selected in ascending cursor order;
        the rank reorders only the page already selected. A STALLED row beyond
        page one stays reachable through next_cursor and leads its own page."""
        rows = [
            _flow_row(1, "IDLE", 9),
            _flow_row(2, "IDLE", 8),
            _flow_row(3, "STARVED", 1),
            _flow_row(4, "IDLE", 7),
            _flow_row(5, "STALLED", 2),
        ]
        cfg = _consumer_flow_cfg(limit=3)
        page_one = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(page_one, {_CONSUMER_FLOW_TOPIC: cfg}) as client:
            first = client.get(
                f"/projection/{_CONSUMER_FLOW_TOPIC}", params={"since": "0"}
            )
        assert first.status_code == 200
        assert [r["projection_cursor"] for r in first.json()["rows"]] == [3, 1, 2]
        assert first.json()["next_cursor"] == "3"
        assert page_one.get_rows.call_args.kwargs["order_by_override"] == (
            ("projection_cursor", "ASC", None),
        )

        page_two = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(page_two, {_CONSUMER_FLOW_TOPIC: cfg}) as client:
            second = client.get(
                f"/projection/{_CONSUMER_FLOW_TOPIC}",
                params={"since": first.json()["next_cursor"]},
            )
        assert second.status_code == 200
        assert [r["projection_cursor"] for r in second.json()["rows"]] == [5, 4]
        assert second.json()["next_cursor"] is None
        assert page_two.get_rows.call_args.kwargs["order_by_override"] == (
            ("projection_cursor", "ASC", None),
        )

    def test_an_unranked_state_fails_closed_instead_of_sorting_last(self) -> None:
        """A flow_state the contract does not rank is refused, never silently
        placed at the bottom of a page where truncation would hide it."""
        rows = [_flow_row(1, "IDLE", 5), _flow_row(2, "DRAINING", 1)]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, {_CONSUMER_FLOW_TOPIC: _consumer_flow_cfg()}) as client:
            resp = client.get(f"/projection/{_CONSUMER_FLOW_TOPIC}")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "unranked_order_value"
        assert body["topic"] == _CONSUMER_FLOW_TOPIC
        assert body["column"] == "flow_state"
        assert "rows" not in body

    def test_caller_order_by_replaces_the_declared_rank(self) -> None:
        """OMN-16290 semantics: a request-time order_by replaces the contract
        default entirely, the rank included, and the report says so."""
        rows = [_flow_row(1, "IDLE", 9), _flow_row(2, "STALLED", 1)]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, {_CONSUMER_FLOW_TOPIC: _consumer_flow_cfg()}) as client:
            resp = client.get(
                f"/projection/{_CONSUMER_FLOW_TOPIC}",
                params={"order_by": "window_end DESC"},
            )
        assert resp.status_code == 200
        assert resp.json()["ordering"] == "window_end DESC"
        assert [r["projection_cursor"] for r in resp.json()["rows"]] == [1, 2]

    def test_order_flip_keeps_the_rank_leading(self) -> None:
        rows = [
            _flow_row(1, "IDLE", 9),
            _flow_row(2, "IDLE", 3),
            _flow_row(3, "STALLED", 1),
        ]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, {_CONSUMER_FLOW_TOPIC: _consumer_flow_cfg()}) as client:
            resp = client.get(
                f"/projection/{_CONSUMER_FLOW_TOPIC}", params={"order": "asc"}
            )
        assert resp.status_code == 200
        assert [r["projection_cursor"] for r in resp.json()["rows"]] == [3, 2, 1]
        assert resp.json()["ordering"].endswith(
            "END ASC, window_end ASC, projection_cursor DESC"
        )

    def test_exposure_without_a_rank_keeps_its_declared_order(self) -> None:
        """No-rank exposures are untouched: same rows, same ordering string."""
        topic = _PR_MERGED_TOPIC
        cfg = _PR_MERGED_CURSOR_MAP[topic].model_copy(
            update={
                "order_by": "projection_cursor DESC",
                "order_by_spec": (("projection_cursor", "DESC", None),),
            }
        )
        assert cfg.order_rank is None
        rows = [{"projection_cursor": "1"}, {"projection_cursor": "2"}]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_cache(cache, {topic: cfg}) as client:
            resp = client.get(f"/projection/{topic}")
        assert resp.status_code == 200
        assert resp.json()["ordering"] == "projection_cursor DESC"
        assert [r["projection_cursor"] for r in resp.json()["rows"]] == ["2", "1"]
