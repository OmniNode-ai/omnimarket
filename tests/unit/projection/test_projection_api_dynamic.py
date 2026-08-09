"""Unit tests for the dynamic projection API (OMN-10490 / OMN-15800).

Tests that the projection API server honours the contract-driven topic map,
serving every route from an in-memory SnapshotCache (OMN-15800 — no DB in
this process):
- Response columns match contract declaration
- DEGRADED entries return 503 with reason
- Unknown topic returns 404 with available topic list
- GET /projections returns full metadata per topic
- An unbootstrapped cache returns 503 snapshot_bootstrap_incomplete
- correlation_id filter on a topic without that column returns 422 (OMN-13165)
- A topic not yet flipped to bus_backed returns 503 not_yet_bus_backed
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from scripts.projection_api_server import (
    app,
    get_snapshot_cache,
    get_topic_map,
    topic_supports_correlation_id_filter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat()


def _order_by_spec(order_by: str | None) -> tuple[tuple[str, str], ...]:
    if order_by is None:
        return ()
    parts = order_by.split()
    column = parts[0]
    direction = parts[1].upper() if len(parts) > 1 else "ASC"
    return ((column, direction),)


def _make_cache(
    rows: list[dict[str, Any]],
    latest_ts: str | None = None,
    *,
    bootstrapped: bool = True,
) -> MagicMock:
    cache = MagicMock()
    cache.is_bootstrapped = MagicMock(return_value=bootstrapped)
    cache.get_rows = MagicMock(return_value=rows)
    parsed_latest = (
        datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
        if latest_ts is not None
        else None
    )
    cache.latest_event_at = MagicMock(return_value=parsed_latest)
    return cache


def _make_cfg(
    topic: str = "test.topic.v1",
    table: str = "test_table",
    columns: tuple[str, ...] = ("col_a", "col_b"),
    order_by: str | None = "col_a DESC",
    freshness_column: str | None = "col_a",
    cursor_column: str | None = None,
    last_event_id_column: str | None = None,
    last_ingest_sequence_column: str | None = None,
    freshness_state_column: str | None = None,
    degraded_reason_column: str | None = None,
    observed_at_column: str | None = None,
    expected_event_interval_seconds: int | None = None,
    status: ProjectionStatus = ProjectionStatus.OK,
    degraded_reason: str = "",
    bus_backed: bool = True,
    key_columns: tuple[str, ...] = ("col_a",),
) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table=table,
        schema_name="public",
        columns=columns,
        order_by=order_by,
        order_by_spec=_order_by_spec(order_by),
        freshness_column=freshness_column,
        expected_event_interval_seconds=expected_event_interval_seconds,
        cursor_column=cursor_column,
        last_event_id_column=last_event_id_column,
        last_ingest_sequence_column=last_ingest_sequence_column,
        freshness_state_column=freshness_state_column,
        degraded_reason_column=degraded_reason_column,
        observed_at_column=observed_at_column,
        limit=100,
        source_contract="node_test",
        status=status,
        degraded_reason=degraded_reason,
        bus_backed=bus_backed,
        key_columns=key_columns if bus_backed else (),
    )


@contextmanager
def _with_overrides(
    cache: MagicMock,
    topic_map: dict[str, ProjectionTableConfig],
) -> Generator[TestClient, None, None]:
    """Override both get_snapshot_cache and get_topic_map; yield a TestClient."""
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


class TestProjectionEndpointDynamic:
    def test_projection_endpoint_uses_declared_columns(self) -> None:
        """Response rows only include columns declared in the contract."""
        declared_cols = ("aggregation_key", "window", "total_cost_usd")
        cfg = _make_cfg(
            topic="onex.snapshot.projection.test.v1",
            columns=declared_cols,
            freshness_column="updated_at",
            key_columns=("aggregation_key",),
        )
        topic_map = {cfg.topic: cfg}
        rows = [
            {
                "aggregation_key": "model-a",
                "window": "daily",
                "total_cost_usd": "1.23",
            }
        ]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_overrides(cache, topic_map) as client:
            resp = client.get("/projection/onex.snapshot.projection.test.v1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 1
        assert body["backing"] == "bus"
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
        cache = _make_cache([])
        with _with_overrides(cache, topic_map) as client:
            resp = client.get("/projection/onex.snapshot.projection.test.v1")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert "not found" in body["reason"]

    def test_unknown_topic_returns_404_with_available_list(self) -> None:
        """Unknown topic returns 404 with all available topics listed."""
        cfg = _make_cfg(topic="known.topic.v1")
        topic_map = {"known.topic.v1": cfg}
        cache = _make_cache([])
        with _with_overrides(cache, topic_map) as client:
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
        cache = _make_cache([])
        with _with_overrides(cache, topic_map) as client:
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
        assert entry["bus_backed"] is True
        assert entry["backing"] == "bus"

    def test_not_yet_bus_backed_topic_returns_503(self) -> None:
        """A topic whose contract has not flipped bus_backed: true returns an
        explicit, self-documenting 503 -- never a stale/absent DB read."""
        cfg = _make_cfg(
            topic="onex.snapshot.projection.test.v1",
            bus_backed=False,
            key_columns=(),
        )
        topic_map = {cfg.topic: cfg}
        cache = _make_cache([])
        with _with_overrides(cache, topic_map) as client:
            resp = client.get("/projection/onex.snapshot.projection.test.v1")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "not_yet_bus_backed"
        assert body["migration_ticket"] == "OMN-15800"
        cache.get_rows.assert_not_called()

    def test_evidence_pipeline_endpoint_returns_projection_envelope(self) -> None:
        cfg = _make_cfg(
            topic="onex.snapshot.projection.evidence_pipeline.stages.v1",
            table="evidence_dashboard_projection",
            columns=(
                "projection_cursor",
                "last_event_id",
                "last_ingest_sequence",
                "freshness_state",
                "degraded_reason",
                "observed_at",
            ),
            order_by="last_ingest_sequence ASC",
            freshness_column="observed_at",
            cursor_column="projection_cursor",
            last_event_id_column="last_event_id",
            last_ingest_sequence_column="last_ingest_sequence",
            freshness_state_column="freshness_state",
            degraded_reason_column="degraded_reason",
            observed_at_column="observed_at",
            key_columns=("projection_cursor",),
        )
        rows = [
            {
                "projection_cursor": "cursor-1",
                "last_event_id": "evt-1",
                "last_ingest_sequence": 10,
                "freshness_state": "CURRENT",
                "degraded_reason": None,
                "observed_at": "2026-05-21T23:00:00Z",
            }
        ]
        cache = _make_cache(rows, latest_ts="2026-05-21T23:00:00Z")

        with _with_overrides(cache, {cfg.topic: cfg}) as client:
            resp = client.get("/v1/evidence-pipeline/stages")

        assert resp.status_code == 200
        body = resp.json()
        assert body["projection_cursor"] == "cursor-1"
        assert body["last_event_id"] == "evt-1"
        assert body["last_ingest_sequence"] == 10
        assert body["freshness_state"] == "CURRENT"
        assert body["version"] == "1.0.0"
        assert body["sse_authority"] == "advisory_only"

    def test_evidence_pipeline_sse_endpoint_is_separate_from_events_snapshot(
        self,
    ) -> None:
        cfg = _make_cfg(
            topic="onex.snapshot.projection.evidence_pipeline.live_events.v1",
            table="evidence_correlation_trace_projection",
            columns=("projection_cursor", "last_event_id"),
            cursor_column="projection_cursor",
            last_event_id_column="last_event_id",
            key_columns=("projection_cursor",),
        )
        cache = _make_cache([])

        with _with_overrides(cache, {cfg.topic: cfg}) as client:
            snapshot_resp = client.get("/v1/evidence-pipeline/events")
            stream_resp = client.get("/v1/evidence-pipeline/events/stream")

        assert snapshot_resp.status_code == 200
        assert stream_resp.status_code == 200
        assert "projection_state_required" in stream_resp.text

    # -----------------------------------------------------------------------
    # OMN-13168: a correlation_id that matches no evidence-pipeline rows must
    # report EMPTY (healthy-but-no-match), NOT DEGRADED.
    # -----------------------------------------------------------------------
    def test_evidence_pipeline_empty_correlation_reports_empty_not_degraded(
        self,
    ) -> None:
        cfg = _make_cfg(
            topic="onex.snapshot.projection.evidence_pipeline.correlations.v1",
            table="evidence_correlation_trace_projection",
            columns=(
                "correlation_id",
                "projection_cursor",
                "last_event_id",
                "last_ingest_sequence",
                "freshness_state",
                "degraded_reason",
                "observed_at",
            ),
            order_by="last_ingest_sequence ASC",
            freshness_column="observed_at",
            cursor_column="projection_cursor",
            last_event_id_column="last_event_id",
            last_ingest_sequence_column="last_ingest_sequence",
            freshness_state_column="freshness_state",
            degraded_reason_column="degraded_reason",
            observed_at_column="observed_at",
            key_columns=("projection_cursor",),
        )
        cache = _make_cache([], latest_ts=None)

        with _with_overrides(cache, {cfg.topic: cfg}) as client:
            resp = client.get(
                "/v1/evidence-pipeline/correlation-traces"
                "?correlation_id=22f52e6a-a6f7-443e-9ea6-196b0a2eb11c"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 0
        assert body["freshness_state"] == "EMPTY", body["freshness_state"]
        assert body["query_scope"] == "evidence_pipeline"
        assert (
            body["authoritative_correlation_source"]
            == "onex.snapshot.projection.delegation.correlation-trace.v1"
        )

    def test_evidence_pipeline_genuinely_stale_table_surfaces_stale(self) -> None:
        """A populated-but-stale table (no filter) keeps a genuine-staleness signal."""
        cfg = _make_cfg(
            topic="onex.snapshot.projection.evidence_pipeline.correlations.v1",
            table="evidence_correlation_trace_projection",
            columns=(
                "correlation_id",
                "projection_cursor",
                "freshness_state",
                "observed_at",
            ),
            order_by="observed_at ASC",
            freshness_column="observed_at",
            cursor_column="projection_cursor",
            freshness_state_column="freshness_state",
            observed_at_column="observed_at",
            expected_event_interval_seconds=300,
            key_columns=("projection_cursor",),
        )
        stale_ts = _ts(timedelta(hours=2))
        rows = [
            {
                "correlation_id": "abc",
                "projection_cursor": "cursor-9",
                "freshness_state": None,
                "observed_at": stale_ts,
            }
        ]
        cache = _make_cache(rows, latest_ts=stale_ts)

        with _with_overrides(cache, {cfg.topic: cfg}) as client:
            resp = client.get("/v1/evidence-pipeline/correlation-traces")

        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 1
        assert body["freshness_state"] == "STALE"

    def test_unbootstrapped_cache_returns_503_degraded(self) -> None:
        """An unbootstrapped SnapshotCache returns 503, never a partial/empty 200
        (OMN-15800 -- the bare 200/row_count:0 that hid OMN-15797)."""
        cfg = _make_cfg(topic="onex.snapshot.projection.test.v1")
        topic_map = {cfg.topic: cfg}
        cache = _make_cache([], bootstrapped=False)
        with _with_overrides(cache, topic_map) as client:
            resp = client.get("/projection/onex.snapshot.projection.test.v1")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "snapshot_bootstrap_incomplete"

    def test_absent_order_by_returns_undefined_ordering(self) -> None:
        """When order_by is None, response includes ordering: undefined."""
        cfg = _make_cfg(
            topic="onex.snapshot.projection.test.v1",
            order_by=None,
            freshness_column="col_a",
        )
        topic_map = {cfg.topic: cfg}
        rows = [{"col_a": "v1", "col_b": "v2"}]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_overrides(cache, topic_map) as client:
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
        cache = _make_cache([], latest_ts=None)
        with _with_overrides(cache, topic_map) as client:
            resp = client.get("/projection/onex.snapshot.projection.test.v1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_freshness"] == "unknown"

    # -----------------------------------------------------------------------
    # OMN-13165: correlation_id filter on topics without that column
    # -----------------------------------------------------------------------

    def test_correlation_id_filter_on_topic_without_column_returns_422(self) -> None:
        """Filtering by correlation_id on a topic whose declared columns do not
        include that column must return a typed 422 (unsupported_filter) before
        touching the cache -- not a 503.
        """
        cfg = _make_cfg(
            topic="onex.snapshot.projection.delegation.summary.v1",
            table="projection_delegation_summary",
            columns=(
                '"totalDelegations"',
                '"qualityGatePassRate"',
                '"totalSavingsUsd"',
                "latest_projection_updated_at",
            ),
            order_by=None,
            freshness_column="latest_projection_updated_at",
            key_columns=("latest_projection_updated_at",),
        )
        topic_map = {cfg.topic: cfg}
        cache = _make_cache([])
        with _with_overrides(cache, topic_map) as client:
            resp = client.get(
                "/projection/onex.snapshot.projection.delegation.summary.v1"
                "?correlation_id=1b90ea27-1f06-42ae-b668-ecdf9450f7ca"
            )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "unsupported_filter"
        assert body["filter"] == "correlation_id"
        assert "delegation.summary.v1" in body["topic"]
        assert "correlation_id" in body["detail"]

    def test_correlation_id_filter_on_topic_with_column_is_allowed(self) -> None:
        """A topic that explicitly declares ``correlation_id`` in its column list
        must accept the filter and return rows (not 422).
        """
        cfg = _make_cfg(
            topic="onex.snapshot.projection.delegation.correlation-trace.v1",
            table="delegation_events",
            columns=(
                "id",
                "correlation_id",
                "task_type",
                "delegated_to",
                "created_at",
            ),
            order_by="created_at ASC",
            freshness_column="created_at",
            key_columns=("id",),
        )
        topic_map = {cfg.topic: cfg}
        rows = [
            {
                "id": "c205cf7f-6c34-497b-a262-2a8be33cd84b",
                "correlation_id": "1b90ea27-1f06-42ae-b668-ecdf9450f7ca",
                "task_type": "summarization",
                "delegated_to": "Qwen3.6-35B-A3B",
                "created_at": _ts(timedelta(minutes=1)),
            }
        ]
        cache = _make_cache(rows, latest_ts=_ts(timedelta(minutes=1)))
        with _with_overrides(cache, topic_map) as client:
            resp = client.get(
                "/projection/onex.snapshot.projection.delegation.correlation-trace.v1"
                "?correlation_id=1b90ea27-1f06-42ae-b668-ecdf9450f7ca"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 1

    def test_correlation_id_filter_on_star_columns_is_allowed(self) -> None:
        """A topic that uses SELECT * (columns = ('*',)) must allow the filter
        because the underlying row shape may include correlation_id even
        though the column list is not enumerated explicitly.
        """
        cfg = _make_cfg(
            topic="onex.snapshot.projection.test.star.v1",
            columns=("*",),
            order_by=None,
            freshness_column=None,
            key_columns=("correlation_id",),
        )
        topic_map = {cfg.topic: cfg}
        cache = _make_cache([{"correlation_id": "abc", "val": 1}])
        with _with_overrides(cache, topic_map) as client:
            resp = client.get(
                "/projection/onex.snapshot.projection.test.star.v1?correlation_id=abc"
            )
        assert resp.status_code == 200


class TestTopicSupportsCorrelationIdFilter:
    """Unit tests for the pure helper (OMN-13165)."""

    def _cfg(self, columns: tuple[str, ...]) -> ProjectionTableConfig:
        return ProjectionTableConfig(
            topic="test.v1",
            table="t",
            schema_name="public",
            columns=columns,
            limit=100,
            source_contract="test",
        )

    def test_explicit_correlation_id_column_returns_true(self) -> None:
        cfg = self._cfg(("id", "correlation_id", "task_type"))
        assert topic_supports_correlation_id_filter(cfg) is True

    def test_missing_correlation_id_column_returns_false(self) -> None:
        cfg = self._cfg(
            (
                '"totalDelegations"',
                '"qualityGatePassRate"',
                "latest_projection_updated_at",
            )
        )
        assert topic_supports_correlation_id_filter(cfg) is False

    def test_star_columns_returns_true(self) -> None:
        cfg = self._cfg(("*",))
        assert topic_supports_correlation_id_filter(cfg) is True

    def test_quoted_correlation_id_column_returns_true(self) -> None:
        cfg = self._cfg(('"correlation_id"', "task_type"))
        assert topic_supports_correlation_id_filter(cfg) is True
