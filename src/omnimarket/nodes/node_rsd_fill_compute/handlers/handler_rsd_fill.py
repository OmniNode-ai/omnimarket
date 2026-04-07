# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler that selects top-N tickets by RSD score.

This is a COMPUTE handler - pure transformation, no I/O.

Related:
    - OMN-7315: node_rsd_fill_compute
    - OMN-7578: Migration to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from omnimarket.nodes.node_rsd_fill_compute.models.model_rsd_fill_output import (
    ModelRsdFillOutput,
)
from omnimarket.nodes.node_rsd_fill_compute.models.model_scored_ticket import (
    ModelScoredTicket,
)

logger = logging.getLogger(__name__)


# Node-local handler classification literals (replaces infra enums)
HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]

HANDLER_TYPE: HandlerType = "NODE_HANDLER"
HANDLER_CATEGORY: HandlerCategory = "COMPUTE"


class HandlerRsdFill:
    """Selects top-N tickets by RSD score with deterministic tie-breaking.

    Tie-break order: higher RSD score > lower priority number (urgent first) > ticket_id ASC.
    """

    @property
    def handler_type(self) -> HandlerType:
        return HANDLER_TYPE

    @property
    def handler_category(self) -> HandlerCategory:
        return HANDLER_CATEGORY

    async def handle(
        self,
        correlation_id: UUID,
        scored_tickets: tuple[ModelScoredTicket, ...],
        max_tickets: int,
    ) -> ModelRsdFillOutput:
        """Select top-N tickets by RSD score with deterministic tie-breaking.

        Args:
            correlation_id: Cycle correlation ID.
            scored_tickets: All candidate tickets with RSD scores.
            max_tickets: Maximum tickets to select.

        Returns:
            ModelRsdFillOutput with selected tickets.
        """
        logger.info(
            "RSD fill: selecting top-%d from %d candidates (correlation_id=%s)",
            max_tickets,
            len(scored_tickets),
            correlation_id,
        )

        # Sort: highest RSD score first, then lowest priority number, then ticket_id ASC
        sorted_tickets = sorted(
            scored_tickets,
            key=lambda t: (-t.rsd_score, t.priority, t.ticket_id),
        )

        selected = tuple(sorted_tickets[:max_tickets])

        logger.info(
            "RSD fill: selected %d tickets [%s]",
            len(selected),
            ", ".join(t.ticket_id for t in selected),
        )

        return ModelRsdFillOutput(
            correlation_id=correlation_id,
            selected_tickets=selected,
            total_candidates=len(scored_tickets),
            total_selected=len(selected),
        )
