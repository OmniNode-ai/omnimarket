# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the morning observability page (OMN-17197, epic OMN-16776).

The page's whole reason to exist is that a projection was live and nothing
rendered it. Its second reason is that when a projection is NOT live, the
render must say so instead of drawing a zero. Both are asserted here:

* a currently-STALLED consumer group is named on the page with its
  ``messages_in``/``messages_out`` (never an aggregate alone);
* an IDLE seam renders with different chrome from a STALLED one;
* the delegation-savings panel, when its exposures refuse, renders the refusal
  code and its tickets and emits **no currency string at all** — the
  "confident zero" regression guard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnimarket.projection.api_server import app, get_snapshot_cache, get_topic_map
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.morning_page import (
    TOPIC_CONSUMER_FLOW,
    TOPIC_COST_SAVINGS_OVERVIEW,
    TOPIC_DELEGATION_SAVINGS,
    TOPIC_DELEGATION_SUMMARY,
    TOPIC_LIVE_EVENTS,
    TOPIC_REGISTRATION,
    EnumPanelState,
    build_flow_panel,
    build_morning_page,
    build_savings_panel,
    latest_window_per_consumer,
    read_projection,
    render_morning_page,
)

pytestmark = pytest.mark.unit


def _flow_cfg(**overrides: Any) -> ProjectionTableConfig:
    base: dict[str, Any] = {
        "topic": TOPIC_CONSUMER_FLOW,
        "table": "consumer_flow_windows",
        "schema_name": "omninode_internal",
        "columns": (
            "consumer_group",
            "topic",
            "window_start",
            "window_end",
            "node_id",
            "ingest_sequence",
            "messages_in",
            "messages_out",
            "messages_dlq",
            "handler_errors",
            "upstream_produced",
            "upstream_evidence",
            "flow_state",
            "evaluated_at",
        ),
        "order_by": "window_end DESC",
        "order_by_spec": (("window_end", "DESC", None),),
        "freshness_column": "window_end",
        "limit": 500,
        "source_contract": "projection_consumer_flow",
        "bus_backed": True,
        "key_columns": ("consumer_group", "topic"),
    }
    base.update(overrides)
    return ProjectionTableConfig(**base)


def _savings_cfg(topic: str, *, bus_backed: bool = False) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table="projection_delegation_savings",
        schema_name="public",
        columns=(
            "cumulative_savings_usd",
            "cumulative_local_cost_usd",
            "cumulative_cloud_cost_usd",
            "baseline_model",
            "pricing_manifest_version",
            "session_count",
            "latest_projection_updated_at",
        ),
        order_by=None,
        freshness_column="latest_projection_updated_at",
        limit=1,
        source_contract="projection_savings",
        bus_backed=bus_backed,
        key_columns=("baseline_model",) if bus_backed else (),
    )


def _window(
    consumer_group: str,
    topic: str,
    state: str,
    *,
    messages_in: int,
    messages_out: int,
    messages_dlq: int = 0,
    handler_errors: int = 0,
    window_end: str = "2026-08-31T10:35:01.228350+00:00",
    ingest_sequence: int = 291,
    upstream_evidence: str = "NONE",
) -> dict[str, Any]:
    return {
        "consumer_group": consumer_group,
        "topic": topic,
        "window_start": "2026-08-31T10:34:31.219533+00:00",
        "window_end": window_end,
        "node_id": "3d56c028-48d1-4258-b01b-2cad762a7bba",
        "ingest_sequence": ingest_sequence,
        "messages_in": messages_in,
        "messages_out": messages_out,
        "messages_dlq": messages_dlq,
        "handler_errors": handler_errors,
        "upstream_produced": None,
        "upstream_evidence": upstream_evidence,
        "flow_state": state,
        "evaluated_at": window_end,
    }


class _FakeCache:
    """Minimal SnapshotCache stand-in: rows, bootstrap state, freshness."""

    def __init__(
        self,
        rows_by_topic: dict[str, list[dict[str, Any]]],
        *,
        unbootstrapped: frozenset[str] = frozenset(),
        latest: datetime | None = None,
    ) -> None:
        self._rows = rows_by_topic
        self._unbootstrapped = unbootstrapped
        self._latest = latest or datetime.now(UTC) - timedelta(seconds=7)

    @property
    def bus_backed_topics(self) -> frozenset[str]:
        return frozenset(self._rows)

    def is_bootstrapped(self, topic: str) -> bool:
        return topic not in self._unbootstrapped

    def latest_event_at(self, topic: str) -> datetime | None:
        return self._latest if self._rows.get(topic) else None

    def row_count(self, topic: str) -> int:
        return len(self._rows.get(topic, []))

    def get_rows(
        self,
        topic: str,
        *,
        limit: int | None = None,
        order_by_override: Any = None,
        tenant_column: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self._rows.get(topic, []))
        return rows if limit is None else rows[:limit]


_LIVE_FLOW_ROWS = [
    _window(
        "local.omnimarket.projection_consumer_flow.consume.1.0.0",
        "onex.evt.platform.node-heartbeat.v1",
        "STALLED",
        messages_in=3,
        messages_out=0,
    ),
    _window(
        "local.omnimarket.alert_channel_liveness_effect.consume.1.0.0",
        "onex.evt.omnimarket.projection-consumer-flow-applied.v1",
        "FLOWING",
        messages_in=3,
        messages_out=3,
    ),
    _window(
        "local.omnibase_infra.node_coding_agent_fsm_reducer.consume.1.0.0",
        "onex.cmd.omnibase_infra.coding-agent.v1",
        "IDLE",
        messages_in=0,
        messages_out=0,
    ),
    _window(
        "local.omnimarket.dlq_carrier.consume.1.0.0",
        "onex.evt.omnimarket.thing.v1",
        "STARVED",
        messages_in=0,
        messages_out=0,
        messages_dlq=4,
        handler_errors=2,
        upstream_evidence="PRODUCED",
    ),
]


def _live_topic_map() -> dict[str, ProjectionTableConfig]:
    return {
        TOPIC_CONSUMER_FLOW: _flow_cfg(),
        TOPIC_DELEGATION_SAVINGS: _savings_cfg(TOPIC_DELEGATION_SAVINGS),
        TOPIC_COST_SAVINGS_OVERVIEW: _savings_cfg(TOPIC_COST_SAVINGS_OVERVIEW),
        TOPIC_DELEGATION_SUMMARY: _savings_cfg(TOPIC_DELEGATION_SUMMARY),
    }


class TestReadRefusalTaxonomy:
    """read_projection must mirror GET /projection/{topic}'s refusals exactly."""

    def test_unknown_topic_is_refused_not_empty(self) -> None:
        read = read_projection("nope.v1", {}, _FakeCache({}), limit=10)
        assert read.state is EnumPanelState.REFUSED
        assert read.reason_code == "unknown_topic"
        assert read.rows == ()

    def test_not_yet_bus_backed_carries_the_migration_ticket(self) -> None:
        topic_map = {TOPIC_DELEGATION_SAVINGS: _savings_cfg(TOPIC_DELEGATION_SAVINGS)}
        read = read_projection(
            TOPIC_DELEGATION_SAVINGS, topic_map, _FakeCache({}), limit=1
        )
        assert read.state is EnumPanelState.REFUSED
        assert read.reason_code == "not_yet_bus_backed"
        assert read.migration_ticket == "OMN-15800"

    def test_contract_degraded_is_distinct_from_not_bus_backed(self) -> None:
        cfg = _flow_cfg(
            status=ProjectionStatus.DEGRADED, degraded_reason="column vanished"
        )
        read = read_projection(
            TOPIC_CONSUMER_FLOW, {TOPIC_CONSUMER_FLOW: cfg}, _FakeCache({}), limit=10
        )
        assert read.reason_code == "contract_degraded"
        assert "column vanished" in read.reason_detail

    def test_unbootstrapped_cache_is_refused_not_zero_rows(self) -> None:
        cache = _FakeCache(
            {TOPIC_CONSUMER_FLOW: _LIVE_FLOW_ROWS},
            unbootstrapped=frozenset({TOPIC_CONSUMER_FLOW}),
        )
        read = read_projection(
            TOPIC_CONSUMER_FLOW, {TOPIC_CONSUMER_FLOW: _flow_cfg()}, cache, limit=10
        )
        assert read.state is EnumPanelState.REFUSED
        assert read.reason_code == "snapshot_bootstrap_incomplete"

    def test_tenant_scoped_exposure_is_refused_never_served_unscoped(self) -> None:
        cfg = ProjectionTableConfig(
            topic="onex.snapshot.projection.tenant-credentials.v1",
            table="tenant_inference_credentials",
            schema_name="public",
            columns=("tenant_id", "provider"),
            limit=10,
            bus_backed=True,
            key_columns=("tenant_id",),
            tenant_column="tenant_id",
        )
        read = read_projection(cfg.topic, {cfg.topic: cfg}, _FakeCache({}), limit=10)
        assert read.state is EnumPanelState.REFUSED
        assert read.reason_code == "tenant_context_unresolved"

    def test_bus_backed_with_no_rows_is_empty_not_refused(self) -> None:
        cfg = ProjectionTableConfig(
            topic=TOPIC_LIVE_EVENTS,
            table="live_events",
            schema_name="public",
            columns=("event_id", "type"),
            limit=10,
            bus_backed=True,
            key_columns=("event_id",),
        )
        read = read_projection(
            TOPIC_LIVE_EVENTS, {TOPIC_LIVE_EVENTS: cfg}, _FakeCache({}), limit=10
        )
        assert read.state is EnumPanelState.EMPTY
        assert read.reason_code == "no_rows"


class TestLatestWindowPerConsumer:
    def test_collapses_to_the_newest_window_per_consumer_topic_pair(self) -> None:
        older = _window(
            "g",
            "t",
            "IDLE",
            messages_in=0,
            messages_out=0,
            window_end="2026-08-31T10:30:00+00:00",
            ingest_sequence=1,
        )
        newer = _window(
            "g",
            "t",
            "STALLED",
            messages_in=9,
            messages_out=0,
            window_end="2026-08-31T10:35:00+00:00",
            ingest_sequence=2,
        )
        collapsed = latest_window_per_consumer((older, newer))
        assert len(collapsed) == 1
        assert collapsed[0].flow_state == "STALLED"
        assert collapsed[0].messages_in == 9

    def test_ingest_sequence_breaks_a_shared_window_boundary(self) -> None:
        first = _window(
            "g", "t", "IDLE", messages_in=0, messages_out=0, ingest_sequence=5
        )
        second = _window(
            "g", "t", "FLOWING", messages_in=2, messages_out=2, ingest_sequence=6
        )
        collapsed = latest_window_per_consumer((first, second))
        assert collapsed[0].flow_state == "FLOWING"

    def test_the_projections_verdict_is_never_regraded(self) -> None:
        """in>0/out==0 is the STALLED shape, but the page renders what it is told."""
        row = _window("g", "t", "FLOWING", messages_in=3, messages_out=0)
        assert latest_window_per_consumer((row,))[0].flow_state == "FLOWING"

    def test_severity_ordering_puts_stalled_first_and_idle_last(self) -> None:
        states = [
            c.flow_state for c in latest_window_per_consumer(tuple(_LIVE_FLOW_ROWS))
        ]
        assert states[0] == "STALLED"
        assert states[-1] == "IDLE"


class TestFlowPanel:
    def test_census_and_dlq_depth_are_summed_from_latest_windows(self) -> None:
        read = read_projection(
            TOPIC_CONSUMER_FLOW,
            {TOPIC_CONSUMER_FLOW: _flow_cfg()},
            _FakeCache({TOPIC_CONSUMER_FLOW: _LIVE_FLOW_ROWS}),
            limit=100,
        )
        panel = build_flow_panel(read)
        assert panel.consumer_count == 4
        assert dict(panel.state_counts) == {
            "STALLED": 1,
            "STARVED": 1,
            "FLOWING": 1,
            "IDLE": 1,
        }
        assert panel.dlq_total == 4
        assert panel.handler_error_total == 2
        assert panel.idle_count == 1
        assert [c.flow_state for c in panel.attention] == [
            "STALLED",
            "STARVED",
            "FLOWING",
        ]

    def test_a_refused_read_yields_zero_counts_but_a_refused_panel_state(self) -> None:
        read = read_projection(TOPIC_CONSUMER_FLOW, {}, _FakeCache({}), limit=10)
        panel = build_flow_panel(read)
        assert panel.consumer_count == 0
        assert panel.read.state is EnumPanelState.REFUSED


class TestSavingsPanel:
    def test_all_exposures_refused_emits_no_metrics(self) -> None:
        topic_map = _live_topic_map()
        cache = _FakeCache({})
        reads = tuple(
            read_projection(topic, topic_map, cache, limit=1)
            for topic in (
                TOPIC_DELEGATION_SAVINGS,
                TOPIC_COST_SAVINGS_OVERVIEW,
                TOPIC_DELEGATION_SUMMARY,
            )
        )
        panel = build_savings_panel(reads)
        assert panel.metrics == ()
        assert panel.has_data is False

    def test_a_live_exposure_emits_only_the_fields_the_row_carries(self) -> None:
        topic_map = _live_topic_map()
        topic_map[TOPIC_DELEGATION_SAVINGS] = _savings_cfg(
            TOPIC_DELEGATION_SAVINGS, bus_backed=True
        )
        cache = _FakeCache(
            {
                TOPIC_DELEGATION_SAVINGS: [
                    {
                        "cumulative_savings_usd": 12.5,
                        "cumulative_local_cost_usd": 0.25,
                        "cumulative_cloud_cost_usd": 12.75,
                        "session_count": 4,
                        "baseline_model": "claude-sonnet-4",
                        # pricing_manifest_version deliberately absent
                    }
                ]
            }
        )
        read = read_projection(TOPIC_DELEGATION_SAVINGS, topic_map, cache, limit=1)
        panel = build_savings_panel((read,))
        labels = {m.label: m.value for m in panel.metrics}
        assert labels["cost avoided"] == "$12.5000"
        assert labels["local cost spent"] == "$0.2500"
        assert labels["cloud baseline (counterfactual)"] == "$12.7500"
        assert labels["delegated sessions"] == "4"
        assert "pricing manifest" not in labels
        assert all(m.source_topic == TOPIC_DELEGATION_SAVINGS for m in panel.metrics)


class TestRenderedPage:
    def _page_html(self) -> str:
        topic_map = _live_topic_map()
        topic_map[TOPIC_REGISTRATION] = ProjectionTableConfig(
            topic=TOPIC_REGISTRATION,
            table="node_service_registry",
            schema_name="public",
            columns=("service_name", "health_status"),
            limit=25,
            bus_backed=True,
            key_columns=("service_name",),
        )
        cache = _FakeCache(
            {
                TOPIC_CONSUMER_FLOW: _LIVE_FLOW_ROWS,
                TOPIC_REGISTRATION: [
                    {
                        "service_name": "node_registration_orchestrator",
                        "health_status": "healthy",
                    }
                ],
            }
        )
        page = build_morning_page(
            topic_map, cache, service_name="omnimarket-projection-api"
        )
        return render_morning_page(page)

    def test_a_stalled_consumer_group_is_named_with_its_in_out_counters(self) -> None:
        """AC3 shape: an aggregate that cannot name the stalled consumer is the
        'one row of zeros' failure this page exists to eliminate."""
        html = self._page_html()
        assert "local.omnimarket.projection_consumer_flow.consume.1.0.0" in html
        assert "onex.evt.platform.node-heartbeat.v1" in html

    def test_idle_renders_with_different_chrome_from_stalled(self) -> None:
        """AC4 shape: identical chrome loses the four-state model at the render
        boundary."""
        html = self._page_html()
        assert "s-STALLED" in html
        assert "s-STARVED" in html
        assert "s-FLOWING" in html
        assert ".s-STALLED{color:var(--stalled)}" in html
        assert ".s-IDLE{color:var(--idle)}" in html

    def test_idle_seams_are_counted_not_listed(self) -> None:
        html = self._page_html()
        assert "IDLE seam(s) not listed" in html
        assert "local.omnibase_infra.node_coding_agent_fsm_reducer" not in html

    def test_refused_savings_panel_shows_the_refusal_and_both_tickets(self) -> None:
        html = self._page_html()
        assert "not_yet_bus_backed" in html
        assert "OMN-15800" in html
        assert "OMN-17298" in html

    def test_refused_savings_panel_emits_no_currency_string(self) -> None:
        """The confident-zero regression guard: no '$' may reach the page when
        every savings exposure refused."""
        html = self._page_html()
        assert "$" not in html

    def test_hostile_row_values_are_escaped(self) -> None:
        topic_map = {TOPIC_CONSUMER_FLOW: _flow_cfg()}
        cache = _FakeCache(
            {
                TOPIC_CONSUMER_FLOW: [
                    _window(
                        "<script>alert(1)</script>",
                        "t",
                        "STALLED",
                        messages_in=1,
                        messages_out=0,
                    )
                ]
            }
        )
        html = render_morning_page(
            build_morning_page(topic_map, cache, service_name="svc")
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_exposure_census_lists_every_discovered_topic(self) -> None:
        html = self._page_html()
        for topic in _live_topic_map():
            assert topic in html
        assert "bus-backed" in html


class TestMorningRoute:
    def test_route_serves_html_with_an_auto_refresh(self) -> None:
        topic_map = _live_topic_map()
        cache = _FakeCache({TOPIC_CONSUMER_FLOW: _LIVE_FLOW_ROWS})
        app.dependency_overrides[get_topic_map] = lambda: topic_map
        app.dependency_overrides[get_snapshot_cache] = lambda: cache
        try:
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/morning")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert 'http-equiv="refresh"' in response.text
        assert "ONEX morning observability" in response.text

    def test_route_renders_200_even_when_every_exposure_refuses(self) -> None:
        """The page IS the report: a 5xx would hide the panels that are fine."""
        app.dependency_overrides[get_topic_map] = lambda: {}
        app.dependency_overrides[get_snapshot_cache] = lambda: _FakeCache({})
        try:
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/morning")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        assert "unknown_topic" in response.text
