"""Golden chain tests for node_ticket_classify_compute.

Verifies keyword-heuristic buildability classification.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.models.enum_buildability import EnumBuildability
from omnimarket.nodes.node_ticket_classify_compute.handlers.handler_ticket_classify import (
    HandlerTicketClassify,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_classify_output import (
    ModelTicketClassifyOutput,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_for_classification import (
    ModelTicketForClassification,
)


def _ticket(
    ticket_id: str,
    title: str,
    description: str = "",
    labels: tuple[str, ...] = (),
    state: str = "",
) -> ModelTicketForClassification:
    return ModelTicketForClassification(
        ticket_id=ticket_id,
        title=title,
        description=description,
        labels=labels,
        state=state,
    )


@pytest.mark.unit
class TestTicketClassifyComputeGoldenChain:
    """Golden chain: tickets -> buildability classifications."""

    async def test_auto_buildable_title_keyword(self) -> None:
        """Ticket with 'implement' in title -> AUTO_BUILDABLE."""
        handler = HandlerTicketClassify()
        tickets = (_ticket("OMN-1", "Implement new handler"),)

        result: ModelTicketClassifyOutput = await handler.handle(
            correlation_id=uuid4(), tickets=tickets
        )

        assert len(result.classifications) == 1
        assert result.classifications[0].buildability == EnumBuildability.AUTO_BUILDABLE
        assert result.total_auto_buildable == 1

    async def test_skip_terminal_state(self) -> None:
        """Ticket in terminal state -> SKIP."""
        handler = HandlerTicketClassify()
        tickets = (_ticket("OMN-1", "Something", state="Done"),)

        result = await handler.handle(correlation_id=uuid4(), tickets=tickets)

        assert result.classifications[0].buildability == EnumBuildability.SKIP
        assert result.total_skipped == 1

    async def test_skip_keyword(self) -> None:
        """Ticket with skip keyword -> SKIP."""
        handler = HandlerTicketClassify()
        tickets = (_ticket("OMN-1", "Work in progress on feature"),)

        result = await handler.handle(correlation_id=uuid4(), tickets=tickets)

        assert result.classifications[0].buildability == EnumBuildability.SKIP

    async def test_blocked_keyword(self) -> None:
        """Ticket with blocked keyword -> BLOCKED."""
        handler = HandlerTicketClassify()
        tickets = (_ticket("OMN-1", "Feature blocked by third-party vendor"),)

        result = await handler.handle(correlation_id=uuid4(), tickets=tickets)

        assert result.classifications[0].buildability == EnumBuildability.BLOCKED

    async def test_arch_decision_without_buildable_title(self) -> None:
        """Ticket with arch keywords but no buildable title -> NEEDS_ARCH_DECISION."""
        handler = HandlerTicketClassify()
        tickets = (
            _ticket(
                "OMN-1",
                "Evaluate options",
                description="Research the best architecture approach",
            ),
        )

        result = await handler.handle(correlation_id=uuid4(), tickets=tickets)

        assert (
            result.classifications[0].buildability
            == EnumBuildability.NEEDS_ARCH_DECISION
        )

    async def test_buildable_title_overrides_arch_description(self) -> None:
        """Title with buildable keyword overrides arch keywords in description."""
        handler = HandlerTicketClassify()
        tickets = (
            _ticket(
                "OMN-1",
                "Implement the design from RFC",
                description="Based on architecture decision from spike",
            ),
        )

        result = await handler.handle(correlation_id=uuid4(), tickets=tickets)

        assert result.classifications[0].buildability == EnumBuildability.AUTO_BUILDABLE

    async def test_empty_tickets(self) -> None:
        """Empty ticket list returns empty output."""
        handler = HandlerTicketClassify()
        result = await handler.handle(correlation_id=uuid4(), tickets=())

        assert len(result.classifications) == 0
        assert result.total_auto_buildable == 0
        assert result.total_skipped == 0
