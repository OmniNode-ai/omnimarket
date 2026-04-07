# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler that classifies tickets by buildability using keyword heuristics.

This is a COMPUTE handler - pure transformation, no I/O.

Related:
    - OMN-7314: node_ticket_classify_compute
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml

from omnimarket.models.enum_buildability import EnumBuildability
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

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _load_buildability_criteria() -> dict[str, object]:
    """Load buildability_criteria from the node contract YAML."""
    with open(_CONTRACT_PATH, encoding="utf-8") as f:
        contract = yaml.safe_load(f)
    return contract.get("buildability_criteria", {})


def _keywords_from_contract(criteria: dict[str, object], key: str) -> frozenset[str]:
    """Extract a keyword frozenset from the contract criteria."""
    raw = criteria.get(key, [])
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(raw)


_CRITERIA = _load_buildability_criteria()

_AUTO_BUILDABLE_KEYWORDS: frozenset[str] = _keywords_from_contract(
    _CRITERIA, "auto_buildable_keywords"
)
_BLOCKED_KEYWORDS: frozenset[str] = _keywords_from_contract(
    _CRITERIA, "blocked_keywords"
)
_BLOCKED_TITLE_ONLY_KEYWORDS: frozenset[str] = _keywords_from_contract(
    _CRITERIA, "blocked_title_only_keywords"
)
_ARCH_DECISION_KEYWORDS: frozenset[str] = _keywords_from_contract(
    _CRITERIA, "arch_decision_keywords"
)
_SKIP_KEYWORDS: frozenset[str] = _keywords_from_contract(_CRITERIA, "skip_keywords")

HandlerType = Literal["NODE_HANDLER", "INFRA_HANDLER", "PROJECTION_HANDLER"]
HandlerCategory = Literal["EFFECT", "COMPUTE", "NONDETERMINISTIC_COMPUTE"]

# "depends on" is checked separately with a smarter pattern that avoids
# false positives from standard sub-task dependency documentation like
# "Depends on: Task 2" or "Depends on: OMN-1234".
_DEPENDS_ON_FALSE_POSITIVE: re.Pattern[str] = re.compile(
    r"\bdepends on\b[:\s]*(task\b|omn-\d)",
    re.IGNORECASE,
)


def _has_real_dependency_blocker(text: str) -> bool:
    """Check if 'depends on' appears as a genuine blocker, not sub-task linking."""
    text_lower = text.lower()
    if "depends on" not in text_lower:
        return False
    if _DEPENDS_ON_FALSE_POSITIVE.search(text):
        return False
    return True


def _match_keywords(text: str, keywords: frozenset[str]) -> tuple[str, ...]:
    """Return matching keywords found in text (case-insensitive)."""
    text_lower = text.lower()
    return tuple(
        kw for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", text_lower)
    )


class HandlerTicketClassify:
    """Classifies tickets into buildability categories using keyword heuristics."""

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    async def handle(
        self,
        correlation_id: UUID,
        tickets: tuple[ModelTicketForClassification, ...],
    ) -> ModelTicketClassifyOutput:
        """Classify tickets by buildability.

        Classification priority:
            1. SKIP — matches skip keywords or state is terminal
            2. BLOCKED — matches blocked keywords
            3. AUTO_BUILDABLE — title contains buildable action verbs
            4. NEEDS_ARCH_DECISION — arch keywords dominate AND no
               buildable keywords in the title
            5. AUTO_BUILDABLE — default fallback for unmatched tickets

        The key insight: tickets whose *title* contains action verbs like
        "add", "implement", "fix" are buildable even if the description
        mentions "design" or "investigate".  NEEDS_ARCH_DECISION only wins
        when the ticket has arch keywords but no buildable signal in the
        title.

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

            # Priority order: SKIP > BLOCKED > (buildable vs arch) > default
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
            title_blocked_matches = _match_keywords(
                ticket.title, _BLOCKED_TITLE_ONLY_KEYWORDS
            )
            blocked_matches = (*blocked_matches, *title_blocked_matches)
            has_dep_blocker = _has_real_dependency_blocker(combined_text)
            if has_dep_blocker:
                blocked_matches = (*blocked_matches, "depends on")
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

            # Check both keyword sets before deciding.  Title-level buildable
            # keywords override description-level arch keywords.
            auto_matches = _match_keywords(combined_text, _AUTO_BUILDABLE_KEYWORDS)
            title_auto_matches = _match_keywords(ticket.title, _AUTO_BUILDABLE_KEYWORDS)
            arch_matches = _match_keywords(combined_text, _ARCH_DECISION_KEYWORDS)

            # Arch wins only when arch keywords present AND no buildable
            # signal in the title.
            if arch_matches and not title_auto_matches:
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
