# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler that classifies tickets by buildability using keyword heuristics.

This is a COMPUTE handler - pure transformation, no I/O.

Related:
    - OMN-7314: node_ticket_classify_compute
    - OMN-7579: Migration to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import logging
import re
from typing import Literal
from uuid import UUID

from omnimarket.nodes.node_ticket_classify_compute.models.enum_buildability import (
    EnumBuildability,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_classification import (
    ModelTicketClassification,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_classify_output import (
    ModelTicketClassifyOutput,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_for_classification import (
    ModelTicketForClassification,
)

logger = logging.getLogger(__name__)

# Keyword sets for heuristic classification
_AUTO_BUILDABLE_KEYWORDS: frozenset[str] = frozenset(
    {
        "add",
        "create",
        "implement",
        "fix",
        "update",
        "refactor",
        "rename",
        "move",
        "extract",
        "wire",
        "register",
        "migrate",
        "test",
        "node",
        "handler",
        "model",
        "enum",
        "compute",
        "effect",
        "reducer",
    }
)

_BLOCKED_KEYWORDS: frozenset[str] = frozenset(
    {
        "blocked",
        "waiting",
        "depends on",
        "dependency",
        "external",
        "third-party",
        "vendor",
    }
)

_ARCH_DECISION_KEYWORDS: frozenset[str] = frozenset(
    {
        "architecture",
        "design",
        "rfc",
        "proposal",
        "decision",
        "evaluate",
        "investigate",
        "spike",
        "research",
        "tradeoff",
    }
)

_SKIP_KEYWORDS: frozenset[str] = frozenset(
    {
        "in progress",
        "in-progress",
        "wip",
        "stale",
        "duplicate",
        "won't fix",
        "wontfix",
    }
)


def _match_keywords(text: str, keywords: frozenset[str]) -> tuple[str, ...]:
    """Return matching keywords found in text (case-insensitive)."""
    text_lower = text.lower()
    return tuple(
        kw for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", text_lower)
    )


class HandlerTicketClassify:
    """Classifies tickets into buildability categories using keyword heuristics."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        correlation_id: UUID,
        tickets: tuple[ModelTicketForClassification, ...],
    ) -> ModelTicketClassifyOutput:
        """Classify tickets by buildability.

        Classification priority (first match wins):
            1. SKIP — matches skip keywords or state is terminal
            2. BLOCKED — matches blocked keywords
            3. NEEDS_ARCH_DECISION — matches architecture keywords
            4. AUTO_BUILDABLE — matches buildable keywords (default)

        Args:
            correlation_id: Cycle correlation ID.
            tickets: Tickets to classify.

        Returns:
            ModelTicketClassifyOutput with all classifications.
        """
        logger.info(
            "Classifying %d tickets (correlation_id=%s)",
            len(tickets),
            correlation_id,
        )

        classifications: list[ModelTicketClassification] = []
        total_auto = 0
        total_skipped = 0

        for ticket in tickets:
            combined_text = (
                f"{ticket.title} {ticket.description} {' '.join(ticket.labels)}"
            )

            # Priority order: SKIP > BLOCKED > NEEDS_ARCH > AUTO_BUILDABLE
            skip_matches = _match_keywords(combined_text, _SKIP_KEYWORDS)
            if skip_matches or ticket.state in ("Done", "Cancelled", "Duplicate"):
                classifications.append(
                    ModelTicketClassification(
                        ticket_id=ticket.ticket_id,
                        title=ticket.title,
                        buildability=EnumBuildability.SKIP,
                        confidence=0.9 if skip_matches else 0.8,
                        matched_keywords=skip_matches,
                        reason=f"Skip: matched {skip_matches}"
                        if skip_matches
                        else f"Skip: terminal state '{ticket.state}'",
                    )
                )
                total_skipped += 1
                continue

            blocked_matches = _match_keywords(combined_text, _BLOCKED_KEYWORDS)
            if blocked_matches:
                classifications.append(
                    ModelTicketClassification(
                        ticket_id=ticket.ticket_id,
                        title=ticket.title,
                        buildability=EnumBuildability.BLOCKED,
                        confidence=0.7,
                        matched_keywords=blocked_matches,
                        reason=f"Blocked: matched {blocked_matches}",
                    )
                )
                total_skipped += 1
                continue

            arch_matches = _match_keywords(combined_text, _ARCH_DECISION_KEYWORDS)
            if arch_matches:
                classifications.append(
                    ModelTicketClassification(
                        ticket_id=ticket.ticket_id,
                        title=ticket.title,
                        buildability=EnumBuildability.NEEDS_ARCH_DECISION,
                        confidence=0.6,
                        matched_keywords=arch_matches,
                        reason=f"Needs arch decision: matched {arch_matches}",
                    )
                )
                total_skipped += 1
                continue

            auto_matches = _match_keywords(combined_text, _AUTO_BUILDABLE_KEYWORDS)
            confidence = min(0.9, 0.3 + 0.1 * len(auto_matches))
            classifications.append(
                ModelTicketClassification(
                    ticket_id=ticket.ticket_id,
                    title=ticket.title,
                    buildability=EnumBuildability.AUTO_BUILDABLE,
                    confidence=confidence,
                    matched_keywords=auto_matches,
                    reason=f"Auto-buildable: matched {auto_matches}"
                    if auto_matches
                    else "Auto-buildable: default classification",
                )
            )
            total_auto += 1

        logger.info(
            "Classification complete: %d auto-buildable, %d skipped",
            total_auto,
            total_skipped,
        )

        return ModelTicketClassifyOutput(
            correlation_id=correlation_id,
            classifications=tuple(classifications),
            total_auto_buildable=total_auto,
            total_skipped=total_skipped,
        )
