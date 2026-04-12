"""Golden chain test for node_dashboard_sweep.

Verifies the handler can classify pages, triage problem domains,
and emit completion events via EventBusInmemory.
"""

from __future__ import annotations

import json

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_dashboard_sweep.handlers.handler_dashboard_sweep import (
    DashboardSweepRequest,
    EnumFixTier,
    EnumPageStatus,
    ModelPageInput,
    NodeDashboardSweep,
)

CMD_TOPIC = "onex.cmd.omnimarket.dashboard-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.dashboard-sweep-completed.v1"


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
