"""Golden chain tests for node_plan_to_tickets.

Verifies plan parsing, structure detection, dependency extraction, cycle detection,
duplicate ID detection, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_plan_to_tickets.handlers.handler_plan_to_tickets import (
    HandlerPlanToTickets,
    ModelPlanToTicketsRequest,
)

CMD_TOPIC = "onex.cmd.omnimarket.plan-to-tickets-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.plan-to-tickets-completed.v1"

_SIMPLE_PLAN = """\
# Feature Implementation Plan

## Task 1: Create database schema

Create the migration file.

## Task 2: Implement API endpoint

Dependencies: Task 1

Build the REST handler.

## Task 3: Add integration tests

Dependencies: Task 1 and Task 2

Write end-to-end tests.
"""

_PHASE_PLAN = """\
# Release Preparation

## Phase 1: Bump versions

Update pyproject.toml versions.

## Phase 2: Pin dependencies

Dependencies: Phase 1

Pin cross-repo deps.
"""


@pytest.mark.unit
class TestPlanToTicketsGoldenChain:
    """Golden chain: plan content -> parse -> entries with dependencies."""

    async def test_task_sections_parsed(self, event_bus: EventBusInmemory) -> None:
        """Task sections are detected and parsed correctly."""
        handler = HandlerPlanToTickets()
        request = ModelPlanToTicketsRequest(plan_content=_SIMPLE_PLAN)
        result = handler.handle(request)

        assert result.status == "parsed"
        assert result.structure_type == "task_sections"
        assert result.entry_count == 3
        assert result.epic_title == "Feature Implementation Plan"
        assert result.entries[0].entry_id == "P1"
        assert result.entries[0].title == "Task 1: Create database schema"

    async def test_dependencies_extracted(self, event_bus: EventBusInmemory) -> None:
        """Dependencies are extracted from content blocks."""
        handler = HandlerPlanToTickets()
        request = ModelPlanToTicketsRequest(plan_content=_SIMPLE_PLAN)
        result = handler.handle(request)

        assert result.entries[0].dependencies == []
        assert result.entries[1].dependencies == ["P1"]
        assert set(result.entries[2].dependencies) == {"P1", "P2"}

    async def test_phase_sections_parsed(self, event_bus: EventBusInmemory) -> None:
        """Phase sections are detected as fallback structure."""
        handler = HandlerPlanToTickets()
        request = ModelPlanToTicketsRequest(plan_content=_PHASE_PLAN)
        result = handler.handle(request)

        assert result.status == "parsed"
        assert result.structure_type == "phase_sections"
        assert result.entry_count == 2
        assert result.entries[1].dependencies == ["P1"]

    async def test_no_structure_returns_error(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Plan without valid structure returns error."""
        handler = HandlerPlanToTickets()
        request = ModelPlanToTicketsRequest(
            plan_content="# Just a heading\n\nSome text without tasks.\n"
        )
        result = handler.handle(request)

        assert result.status == "error"
        assert len(result.validation_errors) >= 1
        assert "No valid structure" in result.validation_errors[0]

    async def test_circular_dependency_detected(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Circular dependencies produce validation error."""
        circular_plan = """\
# Circular Plan

## Task 1: First task

Dependencies: Task 2

Content here.

## Task 2: Second task

Dependencies: Task 1

Content here.
"""
        handler = HandlerPlanToTickets()
        request = ModelPlanToTicketsRequest(plan_content=circular_plan)
        result = handler.handle(request)

        assert result.status == "error"
        assert any("Circular dependency" in e for e in result.validation_errors)

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = HandlerPlanToTickets()
        results_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            request = ModelPlanToTicketsRequest(
                plan_content=payload["plan_content"],
                dry_run=payload.get("dry_run", False),
            )
            result = handler.handle(request)
            result_payload = result.model_dump()
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-plan-to-tickets"
        )

        cmd_payload = json.dumps({"plan_content": _SIMPLE_PLAN}).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["status"] == "parsed"
        assert results_captured[0]["entry_count"] == 3

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates to result."""
        handler = HandlerPlanToTickets()
        request = ModelPlanToTicketsRequest(plan_content=_SIMPLE_PLAN, dry_run=True)
        result = handler.handle(request)

        assert result.dry_run is True
        assert result.status == "parsed"

    async def test_empty_content_detected(self, event_bus: EventBusInmemory) -> None:
        """Entries with empty content produce validation error."""
        empty_plan = """\
# Empty Plan

## Task 1: Has content

Some content here.

## Task 2: No content

## Task 3: Also has content

More content.
"""
        handler = HandlerPlanToTickets()
        request = ModelPlanToTicketsRequest(plan_content=empty_plan)
        result = handler.handle(request)

        assert result.status == "error"
        assert any("no content" in e.lower() for e in result.validation_errors)

    async def test_omn_dependency_passthrough(
        self, event_bus: EventBusInmemory
    ) -> None:
        """OMN-XXXX dependencies pass through unchanged."""
        plan = """\
# Plan with OMN deps

## Task 1: Fix the thing

Dependencies: OMN-1234, OMN-5678

Implementation details.
"""
        handler = HandlerPlanToTickets()
        request = ModelPlanToTicketsRequest(plan_content=plan)
        result = handler.handle(request)

        assert result.status == "parsed"
        assert "OMN-1234" in result.entries[0].dependencies
        assert "OMN-5678" in result.entries[0].dependencies
