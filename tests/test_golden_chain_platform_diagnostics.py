"""Golden chain tests for node_platform_diagnostics.

Uses EventBusInmemory, zero infra, all dry_run=True.
Covers: overall status derivation, dimension filtering, exception safety, event bus wiring.
"""

from __future__ import annotations

import json

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_platform_diagnostics.handlers.handler_platform_diagnostics import (
    HandlerPlatformDiagnostics,
    ModelDiagnosticsRequest,
)
from omnimarket.nodes.node_platform_diagnostics.models.model_diagnostics_result import (
    EnumDiagnosticDimension,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)

CMD_TOPIC = "onex.cmd.omnimarket.platform-diagnostics-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.platform-diagnostics-completed.v1"


@pytest.mark.unit
class TestPlatformDiagnosticsGoldenChain:
    """Golden chain: command → diagnostics → completion event."""

    async def test_dry_run_all_dimensions_completes(
        self, event_bus: EventBusInmemory
    ) -> None:
        """dry_run=True, all 7 dimensions: handler returns result without raising."""
        handler = HandlerPlatformDiagnostics()
        request = ModelDiagnosticsRequest(dry_run=True)
        result = await handler.handle(request)

        assert result.overall_status in (
            EnumReadinessStatus.PASS,
            EnumReadinessStatus.WARN,
            EnumReadinessStatus.FAIL,
        )
        assert len(result.dimensions) == 7
        assert result.run_duration_seconds >= 0.0
        assert result.generated_at is not None

    async def test_dimension_filtering(self, event_bus: EventBusInmemory) -> None:
        """Requesting a subset of dimensions returns only those dimensions."""
        handler = HandlerPlatformDiagnostics()
        request = ModelDiagnosticsRequest(
            dimensions=[
                EnumDiagnosticDimension.GOLDEN_CHAIN,
                EnumDiagnosticDimension.COVERAGE,
            ],
            dry_run=True,
        )
        result = await handler.handle(request)

        assert len(result.dimensions) == 2
        dim_names = {r.dimension for r in result.dimensions}
        assert EnumDiagnosticDimension.GOLDEN_CHAIN in dim_names
        assert EnumDiagnosticDimension.COVERAGE in dim_names

    async def test_fail_status_overrides_warn(
        self, event_bus: EventBusInmemory
    ) -> None:
        """If any dimension is FAIL, overall_status is FAIL regardless of WARNs."""
        handler = HandlerPlatformDiagnostics()
        # CI_STATUS returns WARN in dry_run; other cached dims will WARN too
        # but overall logic: FAIL > WARN — tested via overall derivation
        request = ModelDiagnosticsRequest(dry_run=True)
        result = await handler.handle(request)

        statuses = {r.status for r in result.dimensions}
        if EnumReadinessStatus.FAIL in statuses:
            assert result.overall_status == EnumReadinessStatus.FAIL
        elif EnumReadinessStatus.WARN in statuses:
            assert result.overall_status == EnumReadinessStatus.WARN
        else:
            assert result.overall_status == EnumReadinessStatus.PASS

    async def test_no_dimensions_runs_all_seven(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Empty dimensions list runs all 7 dimensions."""
        handler = HandlerPlatformDiagnostics()
        request = ModelDiagnosticsRequest(dimensions=[], dry_run=True)
        result = await handler.handle(request)

        assert len(result.dimensions) == 7
        dim_set = {r.dimension for r in result.dimensions}
        assert dim_set == set(EnumDiagnosticDimension)

    async def test_dimension_results_never_raise(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All dimension checks return valid ModelDiagnosticDimensionResult, never raise."""
        handler = HandlerPlatformDiagnostics()
        request = ModelDiagnosticsRequest(dry_run=True)
        result = await handler.handle(request)

        for dim_result in result.dimensions:
            assert dim_result.status in (
                EnumReadinessStatus.PASS,
                EnumReadinessStatus.WARN,
                EnumReadinessStatus.FAIL,
            )
            assert dim_result.check_count >= 0
            assert isinstance(dim_result.actionable_items, list)

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus for command/completion flow."""
        handler = HandlerPlatformDiagnostics()
        completions: list[dict] = []

        async def on_command(message: object) -> None:
            request = ModelDiagnosticsRequest(
                dimensions=[EnumDiagnosticDimension.GOLDEN_CHAIN],
                dry_run=True,
            )
            result = await handler.handle(request)
            completion = {
                "overall_status": result.overall_status.value,
                "dimension_count": len(result.dimensions),
            }
            completions.append(completion)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(completion).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-diagnostics"
        )

        await event_bus.publish(CMD_TOPIC, key=None, value=b'{"check": "all"}')

        assert len(completions) == 1
        assert completions[0]["dimension_count"] == 1

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()
