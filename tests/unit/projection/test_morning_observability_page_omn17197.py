# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the ONEX status page (OMN-17197, always-on per OMN-17346).

Epic OMN-16776. The page's whole reason to exist is that a projection was live
and nothing rendered it. Its second reason is that when a projection is NOT
live, the render must say so instead of drawing a zero. Both are asserted here:

* a currently-STALLED consumer group is named on the page with its
  ``messages_in``/``messages_out`` (never an aggregate alone);
* an IDLE seam renders with different chrome from a STALLED one;
* the delegation-savings panel, when its exposures refuse, renders the refusal
  code and its tickets and emits **no currency string at all** — the
  "confident zero" regression guard.

``TestStatusRootRoute`` covers the OMN-17346 half: the page is served at the
root, ``/morning`` renders the identical document rather than redirecting to
it, the ``<meta http-equiv="refresh">`` interval is present and sane, and the
document title carries the lane so a dev tab and a prod tab are not
interchangeable at the tab strip.
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from omnimarket.projection.api_server import app, get_snapshot_cache, get_topic_map
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.morning_page import (
    DEFAULT_REFRESH_SECONDS,
    PAGE_NAME,
    PAGE_TENANT_UUID,
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
    page_title,
    read_projection,
    render_morning_page,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID

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


def _tenant_scoped_cfg(
    topic: str = TOPIC_DELEGATION_SUMMARY, **overrides: Any
) -> ProjectionTableConfig:
    """A bus-backed exposure that declares a tenant_column, as the four
    delegation aggregates do as of OMN-18159."""
    base: dict[str, Any] = {
        "topic": topic,
        "table": "projection_delegation_summary",
        "schema_name": "public",
        "columns": ("tenant_id", "total_events", "latest_projection_updated_at"),
        "freshness_column": "latest_projection_updated_at",
        "limit": 1,
        "source_contract": "projection_delegation",
        "bus_backed": True,
        "key_columns": ("snapshot_grain", "tenant_id"),
        "tenant_column": "tenant_id",
    }
    base.update(overrides)
    return ProjectionTableConfig(**base)


def _aggregate_row(tenant_id: str, total_events: int) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "total_events": total_events,
        "latest_projection_updated_at": "2026-09-11T13:22:50.968064+00:00",
    }


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
        #: Every get_rows call as (topic, tenant_column, tenant_id), so a test
        #: can assert the scoping argument actually reached the cache rather
        #: than only that the returned rows happened to look right.
        self.get_rows_calls: list[tuple[str, str | None, str | None]] = []

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
        self.get_rows_calls.append((topic, tenant_column, tenant_id))
        rows = list(self._rows.get(topic, []))
        if tenant_column is not None:
            # Mirror SnapshotCache.get_rows: a tenant_column with no tenant_id
            # raises rather than falling back to an unscoped read, and the
            # filter compares the ROW's own stored tenant value.
            if tenant_id is None:
                raise ValueError(
                    f"get_rows({topic!r}) was given tenant_column="
                    f"{tenant_column!r} with no tenant_id"
                )
            rows = [row for row in rows if row.get(tenant_column) == tenant_id]
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

    def test_tenant_scoped_exposure_is_refused_when_no_tenant_is_resolved(
        self,
    ) -> None:
        """No resolved tenant is still a refusal -- the fail-closed default.

        OMN-18159 gave the page a tenant of its own, but the absence of one
        must stay a refusal rather than becoming an unscoped read. This is the
        control for the served case below: the same exposure, the same cache,
        differing only in whether a tenant was resolved.
        """
        cfg = _tenant_scoped_cfg()
        read = read_projection(
            cfg.topic, {cfg.topic: cfg}, _FakeCache({}), limit=10, tenant_id=None
        )
        assert read.state is EnumPanelState.REFUSED
        assert read.reason_code == "tenant_context_unresolved"
        assert read.served_tenant_id is None

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

    def test_hostile_flow_state_cannot_break_out_of_the_class_attribute(
        self,
    ) -> None:
        """`flow_state` is an unvalidated snapshot value that also reaches a
        `class` attribute, not only element text.

        `build_flow_panel` passes `flow_state` through verbatim into
        `state_counts`, so a value crafted to close the attribute would inject
        an event handler into a page served without authentication. Escaping the
        text rendering alone is not sufficient — the attribute site needs it too.
        """
        hostile = 'A" onmouseover="alert(1)'
        topic_map = {TOPIC_CONSUMER_FLOW: _flow_cfg()}
        cache = _FakeCache(
            {
                TOPIC_CONSUMER_FLOW: [
                    _window("cg", "t", hostile, messages_in=1, messages_out=0)
                ]
            }
        )
        html = render_morning_page(
            build_morning_page(topic_map, cache, service_name="svc")
        )
        # The breakout: an unescaped site renders `class="tile a" onmouseover=
        # "alert(1)"`, which is a live handler. Escaped, the same bytes stay
        # inside the quoted value. Asserting on the whole class attribute is
        # what distinguishes the two — the escaped text legitimately still
        # contains the substring `onmouseover=` followed by an entity.
        assert 'onmouseover="alert(1)"' not in html
        assert 'class="tile a&quot; onmouseover=&quot;alert(1)"' in html

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
        assert PAGE_NAME in response.text

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


#: Every ISO-8601 UTC instant on the page. Used to neutralise the one value
#: that legitimately differs between two renders taken microseconds apart.
_ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+\+00:00")


class TestStatusRootRoute:
    """OMN-17346: the page is the always-on root surface; /morning is an alias.

    Operator ruling 2026-08-31: *"it should be always on and not require
    /morning. Index.html should be fine."* Root answered ``404`` before this,
    so the only always-up render of the live projections was reachable solely
    by an operator who already knew the path.
    """

    @staticmethod
    def _get(path: str, **kwargs: Any) -> Any:
        topic_map = _live_topic_map()
        cache = _FakeCache({TOPIC_CONSUMER_FLOW: _LIVE_FLOW_ROWS})
        app.dependency_overrides[get_topic_map] = lambda: topic_map
        app.dependency_overrides[get_snapshot_cache] = lambda: cache
        try:
            client = TestClient(app, raise_server_exceptions=True)
            return client.get(path, **kwargs)
        finally:
            app.dependency_overrides.clear()

    def test_root_serves_the_page(self) -> None:
        response = self._get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<!doctype html>" in response.text
        # The panels, not just a shell: a root that 200s with an empty body
        # would satisfy a status-code assertion and still show nothing.
        assert TOPIC_CONSUMER_FLOW in response.text
        assert (
            "local.omnimarket.projection_consumer_flow.consume.1.0.0" in response.text
        )

    def test_morning_renders_the_page_rather_than_redirecting_to_root(self) -> None:
        """A redirect would put a hop in front of every already-published link
        and would double the request count on a page that reloads itself."""
        response = self._get("/morning", follow_redirects=False)
        assert response.status_code == 200
        assert "location" not in response.headers

    def test_alias_and_root_render_the_same_document(self) -> None:
        root = _ISO_INSTANT.sub("<instant>", self._get("/").text)
        alias = _ISO_INSTANT.sub("<instant>", self._get("/morning").text)
        assert root == alias

    def test_refresh_meta_carries_a_sane_interval(self) -> None:
        """Server-rendered auto-refresh, no JS: the meta tag IS the mechanism,
        so an absent or absurd interval is the whole feature failing."""
        match = re.search(
            r'<meta http-equiv="refresh" content="(\d+)">', self._get("/").text
        )
        assert match is not None
        interval = int(match.group(1))
        assert interval == DEFAULT_REFRESH_SECONDS
        assert 5 <= interval <= 60

    def test_refresh_interval_is_overridable_within_the_served_bounds(self) -> None:
        assert (
            '<meta http-equiv="refresh" content="60">'
            in self._get("/", params={"refresh": 60}).text
        )

    def test_title_carries_the_lane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Three lanes serve this page; a tab strip that reads the same on all
        three is how an operator reads prod and believes it is dev."""
        monkeypatch.setenv(
            "OTEL_SERVICE_NAME", "omnimarket-stability-test-projection-api"
        )
        html = self._get("/").text
        assert (
            "<title>ONEX Status — omnimarket-stability-test-projection-api</title>"
            in html
        )
        assert "omnimarket-stability-test-projection-api" in html

    def test_title_is_standing_not_morning_framed(self) -> None:
        html = self._get("/").text
        assert f"<title>{PAGE_NAME} — " in html
        assert "morning" not in html.lower()

    def test_as_of_timestamp_is_rendered_in_the_page_chrome(self) -> None:
        """Staleness has to be visible without scrolling on a page whose whole
        job is telling you whether what you are reading is current."""
        html = self._get("/").text
        header = html[html.index("<header>") : html.index("</header>")]
        match = re.search(
            r'<span class="asof">as of (' + _ISO_INSTANT.pattern + ")<", header
        )
        assert match is not None
        # Parses, and is the render instant — not a hardcoded or fixture value.
        rendered_at = datetime.fromisoformat(match.group(1))
        assert abs((datetime.now(UTC) - rendered_at).total_seconds()) < 60

    def test_page_title_helper_preserves_the_lane_verbatim(self) -> None:
        assert (
            page_title("omnimarket-prod-projection-api")
            == "ONEX Status — omnimarket-prod-projection-api"
        )

    def test_root_renders_200_even_when_every_exposure_refuses(self) -> None:
        app.dependency_overrides[get_topic_map] = lambda: {}
        app.dependency_overrides[get_snapshot_cache] = lambda: _FakeCache({})
        try:
            client = TestClient(app, raise_server_exceptions=True)
            response = client.get("/")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        # The refusal survives the move to root: still a refusal, never a zero.
        assert "unknown_topic" in response.text


class TestPageResolvesAnExplicitTenant:
    """OMN-18159: the page reads tenant-scoped exposures as ONE named tenant.

    Operator ruling, 2026-09-11T10:06:28Z: declare ``tenant_column`` on the
    four delegation aggregates AND make the operator page authenticate, or
    resolve an explicit house tenant, in the same change -- "an
    unauthenticated internal page serving tenant aggregates is the leak, not
    an acceptable reader."

    The page takes the house-tenant half of that ruling rather than the
    authenticate half, because the surface's own founding ruling (OMN-17197,
    OMN-17346) requires it to need no session gate: it is HTML precisely so
    that it is always up. A page that serves one NAMED tenant and says whose
    numbers it is showing is not an unscoped reader; a page that serves
    whatever the cache holds is.
    """

    def test_the_pages_tenant_is_the_house_tenant(self) -> None:
        """Not a second constant with the same value, the same constant.

        A page-local copy of the UUID would be a second place the house
        tenant's identity lives, and the two would diverge the first time one
        moved.
        """
        assert PAGE_TENANT_UUID == HOUSE_TENANT_UUID

    def test_a_resolved_tenant_serves_the_exposure_instead_of_refusing(
        self,
    ) -> None:
        cfg = _tenant_scoped_cfg()
        cache = _FakeCache({cfg.topic: [_aggregate_row(str(HOUSE_TENANT_UUID), 53)]})
        read = read_projection(
            cfg.topic,
            {cfg.topic: cfg},
            cache,
            limit=1,
            tenant_id=HOUSE_TENANT_UUID,
        )
        assert read.state is EnumPanelState.LIVE
        assert read.reason_code == "ok"
        assert read.served_tenant_id == str(HOUSE_TENANT_UUID)
        assert read.rows[0]["total_events"] == 53

    def test_the_scoping_argument_reaches_the_cache(self) -> None:
        """Asserted on the call, not only on the rows.

        A read that returned the right rows because the fixture held only one
        tenant would pass a rows-only assertion while passing no tenant to
        the cache at all.
        """
        cfg = _tenant_scoped_cfg()
        cache = _FakeCache({cfg.topic: [_aggregate_row(str(HOUSE_TENANT_UUID), 53)]})
        read_projection(
            cfg.topic,
            {cfg.topic: cfg},
            cache,
            limit=1,
            tenant_id=HOUSE_TENANT_UUID,
        )
        assert cache.get_rows_calls == [
            (cfg.topic, "tenant_id", str(HOUSE_TENANT_UUID))
        ]

    def test_another_tenants_rows_are_not_served(self) -> None:
        """The discriminating case: two tenants in the cache, one served.

        Positive control is the first assertion -- the other tenant's row IS
        in the fixture and IS returned when the page resolves that tenant --
        so the exclusion below is a filter working, not an empty fixture.
        """
        cfg = _tenant_scoped_cfg()
        other = "11111111-2222-3333-4444-555555555555"
        rows = [
            _aggregate_row(str(HOUSE_TENANT_UUID), 53),
            _aggregate_row(other, 211),
        ]
        cache = _FakeCache({cfg.topic: rows})
        as_other = read_projection(
            cfg.topic, {cfg.topic: cfg}, cache, limit=10, tenant_id=UUID(other)
        )
        assert [row["total_events"] for row in as_other.rows] == [211]

        as_house = read_projection(
            cfg.topic,
            {cfg.topic: cfg},
            _FakeCache({cfg.topic: rows}),
            limit=10,
            tenant_id=HOUSE_TENANT_UUID,
        )
        assert [row["total_events"] for row in as_house.rows] == [53]

    def test_an_unscoped_exposure_is_not_given_a_tenant(self) -> None:
        """Resolving a tenant does not silently scope exposures that declare none.

        ``tenant_column`` is the opt-in. An exposure without one is
        platform-internal by declaration, and filtering it on a column it does
        not have would return nothing and read as a quiet period.
        """
        cfg = _flow_cfg()
        cache = _FakeCache({TOPIC_CONSUMER_FLOW: _LIVE_FLOW_ROWS})
        read = read_projection(
            TOPIC_CONSUMER_FLOW,
            {TOPIC_CONSUMER_FLOW: cfg},
            cache,
            limit=10,
            tenant_id=HOUSE_TENANT_UUID,
        )
        assert read.served_tenant_id is None
        assert cache.get_rows_calls == [(TOPIC_CONSUMER_FLOW, None, None)]
        assert read.state is EnumPanelState.LIVE

    def test_the_built_page_serves_the_summary_panel_rather_than_refusing_it(
        self,
    ) -> None:
        """End to end through build_morning_page, which is what the route calls."""
        topic_map = _live_topic_map()
        topic_map[TOPIC_DELEGATION_SUMMARY] = _tenant_scoped_cfg()
        cache = _FakeCache(
            {
                TOPIC_CONSUMER_FLOW: _LIVE_FLOW_ROWS,
                TOPIC_DELEGATION_SUMMARY: [_aggregate_row(str(HOUSE_TENANT_UUID), 53)],
            }
        )
        page = build_morning_page(topic_map, cache, service_name="omnimarket-x")
        summary = next(
            read
            for read in page.savings.reads
            if read.topic == TOPIC_DELEGATION_SUMMARY
        )
        assert summary.reason_code != "tenant_context_unresolved"
        assert summary.state is EnumPanelState.LIVE
        assert summary.served_tenant_id == str(HOUSE_TENANT_UUID)

    def test_the_render_names_the_tenant_it_is_showing(self) -> None:
        """A page that scopes silently is a page whose numbers cannot be read.

        An operator looking at a tenant's aggregate must be able to see that
        it is a tenant's aggregate, or they will read it as the platform's --
        which is the misreading OMN-18139 corrected in the contract and which
        the render must not reintroduce.
        """
        topic_map = _live_topic_map()
        topic_map[TOPIC_DELEGATION_SUMMARY] = _tenant_scoped_cfg()
        cache = _FakeCache(
            {
                TOPIC_CONSUMER_FLOW: _LIVE_FLOW_ROWS,
                TOPIC_DELEGATION_SUMMARY: [_aggregate_row(str(HOUSE_TENANT_UUID), 53)],
            }
        )
        html_out = render_morning_page(
            build_morning_page(topic_map, cache, service_name="omnimarket-x")
        )
        assert str(HOUSE_TENANT_UUID) in html_out


class TestDelegationAggregatesDeclareTheirTenant:
    """The contract half of the same ruling, asserted against the file itself."""

    def test_all_four_aggregate_exposures_declare_tenant_column(self) -> None:
        import yaml

        contract = yaml.safe_load(
            (
                pathlib.Path(__file__).resolve().parents[3]
                / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
            ).read_text()
        )
        aggregates = {
            "projection_delegation_summary",
            "projection_delegation_model_routing",
            "projection_delegation_quality_gate",
            "projection_delegation_token_usage",
        }
        exposures = {
            exposure["table"]: exposure
            for exposure in contract["projection_api"]["exposures"]
            if exposure.get("table") in aggregates
        }
        assert set(exposures) == aggregates, "positive control: all four are declared"
        for table, exposure in sorted(exposures.items()):
            assert exposure.get("tenant_column") == "tenant_id", table
            assert "tenant_id" in exposure["columns"], table
