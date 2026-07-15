# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Honest deferred semantic-review adapter (OMN-14529).

decision_store's SKILL.md documents an async LLM (DeepSeek-R1) semantic
conflict check that runs when structural confidence >= 0.6. No such LLM
integration exists anywhere in this repo — building it is genuinely out of
scope for a routing-wiring ticket. This adapter is an honest placeholder: it
passes the structural conflicts through UNCHANGED and leaves
``semantic_checked=False`` on every result, so nothing downstream can
mistake a structural-only conflict for a semantically-reviewed one.

``HandlerDecisionStoreOrchestrator`` only calls this adapter when a conflict
crosses the 0.6 structural-confidence threshold — for the common case (a
decision recorded into a domain/layer with no conflicting history), it is
never invoked at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.nodes.node_decision_store_orchestrator.models.model_decision_store_request import (
    ModelConflictResult,
    ModelDecisionEntry,
)


class DeferredSemanticReview:
    """Real ``ProtocolDecisionSemanticReviewAdapter`` implementation.

    "Real" in the sense that it is a genuine, non-crashing default the
    generic dispatch can construct with zero arguments — not in the sense
    that it performs semantic review. See module docstring.
    """

    def review_conflicts(
        self,
        entry: ModelDecisionEntry,
        conflicts: tuple[ModelConflictResult, ...],
    ) -> list[Mapping[str, Any] | ModelConflictResult]:
        return list(conflicts)


__all__ = ["DeferredSemanticReview"]
