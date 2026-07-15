# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Honest log-only notify adapter (OMN-14529).

decision_store's SKILL.md documents a Slack conflict-resolution gate that
blocks the pipeline until an operator replies with proceed/hold/dismiss. No
Slack integration for decision_store exists anywhere in this repo — building
it is genuinely out of scope for a routing-wiring ticket. This adapter logs
every HIGH-severity conflict at WARNING level (visible, actionable,
searchable) instead of silently dropping it or pretending a gate exists.

``HandlerDecisionStoreOrchestrator`` only calls this adapter when at least
one HIGH-severity conflict was detected — for the common case (no conflict,
or conflicts below HIGH), it is never invoked at all.
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_decision_store_orchestrator.models.model_decision_store_request import (
    ModelConflictResult,
    ModelDecisionEntry,
)

logger = logging.getLogger(__name__)


class LogOnlyNotify:
    """Real ``ProtocolDecisionNotifyAdapter`` implementation.

    "Real" in the sense that it is a genuine, non-crashing default the
    generic dispatch can construct with zero arguments — not in the sense
    that it implements the documented Slack gate. See module docstring.
    """

    def notify_high_conflicts(
        self,
        entry: ModelDecisionEntry,
        conflicts: tuple[ModelConflictResult, ...],
    ) -> None:
        logger.warning(
            "decision_store: %d HIGH-severity conflict(s) for domain=%s "
            "layer=%s summary=%r — Slack conflict-resolution gate "
            "documented in SKILL.md is NOT implemented; logging only "
            "(OMN-14529 follow-up)",
            len(conflicts),
            entry.domain,
            entry.layer.value,
            entry.summary,
            extra={
                "conflict_ids": [c.conflict_id for c in conflicts],
            },
        )


__all__ = ["LogOnlyNotify"]
