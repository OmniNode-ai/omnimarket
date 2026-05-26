"""Golden chain test for node_dashboard_sweep.

Verifies the handler can classify pages, triage problem domains,
and emit completion events via EventBusInmemory.

Also covers Phase 1 HTTP recon via a lightweight stub that replaces
_fetch_page so no live network calls are required in unit tests.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_dashboard_sweep.handlers.handler_dashboard_sweep import (
    DashboardSweepRequest,
    EnumFixTier,
    EnumPageStatus,
    ModelPageInput,
    ModelReconResult,
    NodeDashboardSweep,
)

CMD_TOPIC = "onex.cmd.omnimarket.dashboard-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.dashboard-sweep-completed.v1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOME_HTML = """<!DOCTYPE html>
<html><body>
  <nav>
    <a href="/agents">Agents</a>
    <a href="/events">Events</a>
  </nav>
</body></html>"""

_NORMAL_BODY = "<html><body><p>Real content here</p></body></html>"


def _stub_fetch(url: str) -> tuple[int, str, int, str]:
    """Return deterministic HTTP responses keyed on URL suffix."""
    if url.endswith("/"):
        return 200, "text/html", len(_HOME_HTML), _HOME_HTML
    if url.endswith("/error-page"):
        return 500, "text/html", 50, "500 Internal Server Error"
    if url.endswith("/mock-page"):
        return 200, "text/html", 80, "Sample Agent count: 42 placeholder"
    return 200, "text/html", len(_NORMAL_BODY), _NORMAL_BODY


# ---------------------------------------------------------------------------
# Classification tests (pass-through / pre-classified pages mode)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDashboardSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_healthy_page(self, event_bus: EventBusInmemory) -> None:
        """A page with real data and live timestamps should be HEALTHY."""
        handler = NodeDashboardSweep()
        request = DashboardSweepRequest(
            pages=[
                ModelPageInput(
                    route="/agents",
                    has_data=True,
                    has_live_timestamps=True,
                )
            ]
        )
        result = handler.handle(request)

        assert result.status == "clean"
        assert result.page_statuses[0].status == EnumPageStatus.HEALTHY

    async def test_broken_page_js_error(self, event_bus: EventBusInmemory) -> None:
        """A page with JS errors should be BROKEN."""
        handler = NodeDashboardSweep()
        request = DashboardSweepRequest(
            pages=[
                ModelPageInput(
                    route="/events",
                    has_js_errors=True,
                )
            ]
        )
        result = handler.handle(request)

        assert result.status == "issues_found"
        assert result.page_statuses[0].status == EnumPageStatus.BROKEN
        assert len(result.domains) == 1
        assert result.domains[0].fix_tier == EnumFixTier.CODE_BUG

    async def test_empty_page(self, event_bus: EventBusInmemory) -> None:
        """A page with no data should be EMPTY."""
        handler = NodeDashboardSweep()
        request = DashboardSweepRequest(pages=[ModelPageInput(route="/metrics")])
        result = handler.handle(request)

        assert result.page_statuses[0].status == EnumPageStatus.EMPTY
        assert len(result.domains) == 1
        assert result.domains[0].fix_tier == EnumFixTier.DATA_PIPELINE

    async def test_mock_page_detected(self, event_bus: EventBusInmemory) -> None:
        """A page with mock patterns should be MOCK."""
        handler = NodeDashboardSweep()
        request = DashboardSweepRequest(
            pages=[
                ModelPageInput(
                    route="/settings",
                    has_mock_patterns=True,
                )
            ]
        )
        result = handler.handle(request)

        assert result.page_statuses[0].status == EnumPageStatus.MOCK

    async def test_mock_text_detection(self, event_bus: EventBusInmemory) -> None:
        """Mock patterns in visible text should trigger MOCK classification."""
        handler = NodeDashboardSweep()
        request = DashboardSweepRequest(
            pages=[
                ModelPageInput(
                    route="/dashboard",
                    visible_text="Sample Agent with count: 42 results",
                )
            ]
        )
        result = handler.handle(request)

        assert result.page_statuses[0].status == EnumPageStatus.MOCK

    async def test_flag_gated_page(self, event_bus: EventBusInmemory) -> None:
        """A page with feature flag should be FLAG_GATED."""
        handler = NodeDashboardSweep()
        request = DashboardSweepRequest(
            pages=[
                ModelPageInput(
                    route="/intelligence",
                    has_feature_flag=True,
                )
            ]
        )
        result = handler.handle(request)

        assert result.page_statuses[0].status == EnumPageStatus.FLAG_GATED

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = NodeDashboardSweep()
        results_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            pages = [ModelPageInput(**p) for p in payload.get("pages", [])]
            request = DashboardSweepRequest(pages=pages)
            result = handler.handle(request)
            result_payload = {
                "status": result.status,
                "pages_total": result.pages_total,
            }
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-dashboard"
        )

        cmd_payload = json.dumps(
            {"pages": [{"route": "/", "has_data": True, "has_live_timestamps": True}]}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["status"] == "clean"

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_by_status_counts(self, event_bus: EventBusInmemory) -> None:
        """by_status should aggregate page classifications correctly."""
        handler = NodeDashboardSweep()
        request = DashboardSweepRequest(
            pages=[
                ModelPageInput(route="/a", has_data=True, has_live_timestamps=True),
                ModelPageInput(route="/b", has_js_errors=True),
                ModelPageInput(route="/c"),
            ]
        )
        result = handler.handle(request)

        assert result.by_status.get("HEALTHY", 0) == 1
        assert result.by_status.get("BROKEN", 0) == 1
        assert result.by_status.get("EMPTY", 0) == 1

    async def test_dry_run_flag(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag should propagate from request to result."""
        handler = NodeDashboardSweep()
        request = DashboardSweepRequest(pages=[], dry_run=True)
        result = handler.handle(request)

        assert result.dry_run is True

    async def test_network_error_is_broken(self, event_bus: EventBusInmemory) -> None:
        """Network errors should classify as BROKEN."""
        handler = NodeDashboardSweep()
        request = DashboardSweepRequest(
            pages=[ModelPageInput(route="/api", has_network_errors=True)]
        )
        result = handler.handle(request)

        assert result.page_statuses[0].status == EnumPageStatus.BROKEN

    async def test_recon_results_empty_without_base_url(
        self, event_bus: EventBusInmemory
    ) -> None:
        """recon_results should be empty when no base_url is supplied."""
        handler = NodeDashboardSweep()
        request = DashboardSweepRequest(
            pages=[
                ModelPageInput(route="/agents", has_data=True, has_live_timestamps=True)
            ]
        )
        result = handler.handle(request)

        assert result.recon_results == []


# ---------------------------------------------------------------------------
# Recon phase tests (Phase 1 HTTP recon — _fetch_page stubbed)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDashboardSweepRecon:
    """Unit tests for Phase 1 HTTP recon logic (network calls stubbed)."""

    def test_recon_discovers_routes_from_home_html(self) -> None:
        """Phase 1 discovers routes from <a href> links in the home page."""
        handler = NodeDashboardSweep()
        module_path = (
            "omnimarket.nodes.node_dashboard_sweep"
            ".handlers.handler_dashboard_sweep._fetch_page"
        )
        with patch(module_path, side_effect=_stub_fetch):
            results = handler._run_recon("http://localhost:3000", [])

        routes = {r.route for r in results}
        # /agents and /events are in _HOME_HTML links.
        assert "/agents" in routes
        assert "/events" in routes
        # Known default routes are always included.
        assert "/" in routes

    def test_recon_5xx_sets_network_error_on_page_input(self) -> None:
        """A 500 response from recon should produce has_network_errors=True."""
        handler = NodeDashboardSweep()
        recon = ModelReconResult(
            route="/error-page",
            status_code=500,
            content_type="text/html",
            body_size=50,
            body_snippet="500 Internal Server Error",
            has_error_text=True,
        )
        page_input = handler._recon_to_page_input(recon)

        assert page_input.has_network_errors is True
        assert page_input.has_js_errors is False

    def test_recon_error_text_sets_js_error_when_status_ok(self) -> None:
        """has_error_text on a 200 response should produce has_js_errors=True."""
        handler = NodeDashboardSweep()
        recon = ModelReconResult(
            route="/broken-js",
            status_code=200,
            content_type="text/html",
            body_size=80,
            body_snippet="<!DOCTYPE html>...ChunkLoadError: loading chunk 42 failed",
            has_error_text=True,
        )
        page_input = handler._recon_to_page_input(recon)

        assert page_input.has_js_errors is True
        assert page_input.has_network_errors is False

    def test_recon_mock_body_sets_mock_flag(self) -> None:
        """Mock patterns in the recon body snippet should set has_mock_patterns."""
        handler = NodeDashboardSweep()
        recon = ModelReconResult(
            route="/mock-page",
            status_code=200,
            content_type="text/html",
            body_size=60,
            body_snippet="Sample Agent with count: 42 placeholder",
            has_error_text=False,
        )
        page_input = handler._recon_to_page_input(recon)

        assert page_input.has_mock_patterns is True

    def test_recon_integration_with_base_url(self) -> None:
        """Full handle() with base_url stub: recon_results populated, pages classified."""
        handler = NodeDashboardSweep()
        module_path = (
            "omnimarket.nodes.node_dashboard_sweep"
            ".handlers.handler_dashboard_sweep._fetch_page"
        )
        with patch(module_path, side_effect=_stub_fetch):
            request = DashboardSweepRequest(
                base_url="http://localhost:3000",
                dry_run=True,
            )
            result = handler.handle(request)

        assert len(result.recon_results) > 0
        assert result.pages_total > 0
        # All discovered pages have a classification.
        assert len(result.page_statuses) == result.pages_total

    def test_pre_supplied_pages_take_priority_over_recon(self) -> None:
        """Pre-supplied pages should not be overwritten by recon for the same route."""
        handler = NodeDashboardSweep()
        module_path = (
            "omnimarket.nodes.node_dashboard_sweep"
            ".handlers.handler_dashboard_sweep._fetch_page"
        )
        with patch(module_path, side_effect=_stub_fetch):
            # /agents would be discovered by recon with no semantic flags set,
            # but we pre-supply it as HEALTHY (has_data+has_live_timestamps).
            request = DashboardSweepRequest(
                base_url="http://localhost:3000",
                pages=[
                    ModelPageInput(
                        route="/agents",
                        has_data=True,
                        has_live_timestamps=True,
                    )
                ],
            )
            result = handler.handle(request)

        # Find the /agents classification.
        agents_status = next(ps for ps in result.page_statuses if ps.route == "/agents")
        assert agents_status.status == EnumPageStatus.HEALTHY

    def test_recon_unreachable_host_returns_network_error(self) -> None:
        """A connection failure (status_code=0) maps to has_network_errors=True."""
        handler = NodeDashboardSweep()
        recon = ModelReconResult(
            route="/unreachable",
            status_code=0,
            content_type="",
            body_size=0,
            body_snippet="",
            has_error_text=False,
        )
        page_input = handler._recon_to_page_input(recon)

        assert page_input.has_network_errors is True

    def test_extra_routes_included_in_recon(self) -> None:
        """extra_routes supplied to the request are probed during recon."""
        handler = NodeDashboardSweep()
        module_path = (
            "omnimarket.nodes.node_dashboard_sweep"
            ".handlers.handler_dashboard_sweep._fetch_page"
        )
        with patch(module_path, side_effect=_stub_fetch):
            results = handler._run_recon("http://localhost:3000", ["/custom-route"])

        routes = {r.route for r in results}
        assert "/custom-route" in routes
