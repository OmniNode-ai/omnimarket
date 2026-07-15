"""Golden chain tests for node_create_ticket.

Verifies input validation, seam signal detection, description generation,
dry_run mode, EventBusInmemory wiring, and — since OMN-14547 — the real
Linear create-ticket call via an injected mock client (the handler no longer
fakes ``status="created"`` with an empty ``ticket_id``).
"""

from __future__ import annotations

import json

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    ModelCreateTicketRequest,
)

CMD_TOPIC = "onex.cmd.omnimarket.create-ticket-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.create-ticket-completed.v1"


class _MockLinearTicketClient:
    """Minimal injectable Linear client for unit tests.

    Tracks create_ticket calls; never touches the network.
    """

    def __init__(
        self,
        created_id: str = "OMN-90001",
        created_url: str = "https://linear.app/omninode/issue/OMN-90001",
    ) -> None:
        self._created_id = created_id
        self._created_url = created_url
        self.create_ticket_calls: list[dict[str, object]] = []

    def create_ticket(
        self, *, title: str, description: str, team: str, parent: str | None
    ) -> tuple[str, str]:
        self.create_ticket_calls.append(
            {"title": title, "description": description, "team": team, "parent": parent}
        )
        return self._created_id, self._created_url


@pytest.mark.unit
class TestCreateTicketGoldenChain:
    """Golden chain: request -> validation -> seam detection -> Linear create -> result."""

    async def test_simple_ticket_created(self, event_bus: EventBusInmemory) -> None:
        """A simple ticket with title produces status=created with a real ticket_id."""
        handler = HandlerCreateTicket(linear_client=_MockLinearTicketClient())
        request = ModelCreateTicketRequest(title="Add rate limiting to API")
        result = handler.handle(request)

        assert result.status == "created"
        assert result.title == "Add rate limiting to API"
        assert result.team == "Omninode"
        assert result.ticket_id == "OMN-90001"
        assert result.ticket_url == "https://linear.app/omninode/issue/OMN-90001"
        assert len(result.validation_errors) == 0

    async def test_seam_signals_detected(self, event_bus: EventBusInmemory) -> None:
        """Kafka-related keywords trigger seam detection."""
        handler = HandlerCreateTicket(linear_client=_MockLinearTicketClient())
        request = ModelCreateTicketRequest(
            title="Add Kafka consumer for new topic",
            description="Implement a consumer that subscribes to the events topic.",
        )
        result = handler.handle(request)

        assert result.is_seam_ticket is True
        assert "topics" in result.interfaces_touched
        assert result.contract_completeness == "full"
        assert result.ticket_id == "OMN-90001"

    async def test_non_seam_ticket_is_stub(self, event_bus: EventBusInmemory) -> None:
        """Non-seam tickets get stub contract completeness."""
        handler = HandlerCreateTicket(linear_client=_MockLinearTicketClient())
        request = ModelCreateTicketRequest(
            title="Fix typo in README",
        )
        result = handler.handle(request)

        assert result.is_seam_ticket is False
        assert result.interfaces_touched == []
        assert result.contract_completeness == "stub"
        assert result.ticket_id == "OMN-90001"

    async def test_invalid_parent_id_format(self, event_bus: EventBusInmemory) -> None:
        """Invalid parent ID format produces validation error (no Linear call)."""
        handler = HandlerCreateTicket()
        request = ModelCreateTicketRequest(
            title="Some feature",
            parent="INVALID-ID",
        )
        result = handler.handle(request)

        assert result.status == "error"
        assert len(result.validation_errors) >= 1
        assert "parent ID format" in result.validation_errors[0]

    async def test_invalid_blocked_by_format(self, event_bus: EventBusInmemory) -> None:
        """Invalid blocked_by ID produces validation error (no Linear call)."""
        handler = HandlerCreateTicket()
        request = ModelCreateTicketRequest(
            title="Some feature",
            blocked_by=["OMN-1234", "BAD"],
        )
        result = handler.handle(request)

        assert result.status == "error"
        assert any("blocked_by" in e for e in result.validation_errors)

    async def test_dry_run_mode(self, event_bus: EventBusInmemory) -> None:
        """dry_run produces status=dry_run without errors and without a Linear call."""
        handler = HandlerCreateTicket()
        request = ModelCreateTicketRequest(
            title="New feature",
            dry_run=True,
        )
        result = handler.handle(request)

        assert result.status == "dry_run"
        assert result.dry_run is True
        assert result.ticket_id == ""

    async def test_description_body_includes_summary(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Generated description body includes summary and DoD sections."""
        handler = HandlerCreateTicket(linear_client=_MockLinearTicketClient())
        request = ModelCreateTicketRequest(
            title="Add caching layer",
            repo="omnibase_infra",
            description="Implement Redis caching for hot paths.",
        )
        result = handler.handle(request)

        assert "## Summary" in result.description_body
        assert "Redis caching" in result.description_body
        assert "## Definition of Done" in result.description_body
        assert "omnibase_infra" in result.description_body

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = HandlerCreateTicket(linear_client=_MockLinearTicketClient())
        results_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            request = ModelCreateTicketRequest(
                title=payload["title"],
                repo=payload.get("repo"),
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
            CMD_TOPIC, on_message=on_command, group_id="test-create-ticket"
        )

        cmd_payload = json.dumps({"title": "Test ticket"}).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["status"] == "created"
        assert results_captured[0]["ticket_id"] == "OMN-90001"

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_multiple_seam_interfaces(self, event_bus: EventBusInmemory) -> None:
        """Multiple seam signals detected from different categories."""
        handler = HandlerCreateTicket(linear_client=_MockLinearTicketClient())
        request = ModelCreateTicketRequest(
            title="Add API endpoint for Kafka consumer status",
            description="REST endpoint to query consumer group health.",
        )
        result = handler.handle(request)

        assert result.is_seam_ticket is True
        assert "topics" in result.interfaces_touched
        assert "public_api" in result.interfaces_touched

    async def test_valid_parent_id_accepted(self, event_bus: EventBusInmemory) -> None:
        """Valid OMN-XXXX parent ID passes validation and reaches the Linear call."""
        client = _MockLinearTicketClient()
        handler = HandlerCreateTicket(linear_client=client)
        request = ModelCreateTicketRequest(
            title="Sub-task",
            parent="OMN-5678",
        )
        result = handler.handle(request)

        assert result.status == "created"
        assert len(result.validation_errors) == 0
        assert client.create_ticket_calls[0]["parent"] == "OMN-5678"
