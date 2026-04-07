"""Golden chain tests for node_rsd_fill_compute.

Verifies deterministic top-N selection by RSD score with tie-breaking.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_rsd_fill_compute.handlers.handler_rsd_fill import (
    HandlerRsdFill,
)
from omnimarket.nodes.node_rsd_fill_compute.models.model_rsd_fill_output import (
    ModelRsdFillOutput,
)
from omnimarket.nodes.node_rsd_fill_compute.models.model_scored_ticket import (
    ModelScoredTicket,
)


def _ticket(
    ticket_id: str, rsd_score: float, priority: int = 0
) -> ModelScoredTicket:
    return ModelScoredTicket(
        ticket_id=ticket_id,
        title=f"Ticket {ticket_id}",
        rsd_score=rsd_score,
        priority=priority,
    )


@pytest.mark.unit
class TestRsdFillComputeGoldenChain:
    """Golden chain: scored tickets -> top-N selection."""

    async def test_selects_top_n_by_rsd_score(self) -> None:
        """Highest RSD scores are selected first."""
        handler = HandlerRsdFill()
        tickets = (
            _ticket("OMN-1", 5.0),
            _ticket("OMN-2", 10.0),
            _ticket("OMN-3", 3.0),
            _ticket("OMN-4", 8.0),
        )

        result: ModelRsdFillOutput = await handler.handle(
            correlation_id=uuid4(),
            scored_tickets=tickets,
            max_tickets=2,
        )

        assert result.total_candidates == 4
        assert result.total_selected == 2
        assert result.selected_tickets[0].ticket_id == "OMN-2"
        assert result.selected_tickets[1].ticket_id == "OMN-4"

    async def test_deterministic_tie_break(self) -> None:
        """Tied RSD scores break by priority then ticket_id."""
        handler = HandlerRsdFill()
        tickets = (
            _ticket("OMN-3", 5.0, priority=2),
            _ticket("OMN-1", 5.0, priority=1),
            _ticket("OMN-2", 5.0, priority=1),
        )

        result = await handler.handle(
            correlation_id=uuid4(),
            scored_tickets=tickets,
            max_tickets=3,
        )

        ids = [t.ticket_id for t in result.selected_tickets]
        # priority=1 first (lower number = higher urgency), then ticket_id ASC
        assert ids == ["OMN-1", "OMN-2", "OMN-3"]

    async def test_empty_input(self) -> None:
        """Empty ticket list returns empty output."""
        handler = HandlerRsdFill()
        result = await handler.handle(
            correlation_id=uuid4(),
            scored_tickets=(),
            max_tickets=5,
        )
        assert result.total_candidates == 0
        assert result.total_selected == 0
        assert result.selected_tickets == ()

    async def test_max_tickets_exceeds_candidates(self) -> None:
        """When max_tickets > candidates, returns all candidates."""
        handler = HandlerRsdFill()
        tickets = (_ticket("OMN-1", 5.0), _ticket("OMN-2", 3.0))

        result = await handler.handle(
            correlation_id=uuid4(),
            scored_tickets=tickets,
            max_tickets=10,
        )
        assert result.total_selected == 2
