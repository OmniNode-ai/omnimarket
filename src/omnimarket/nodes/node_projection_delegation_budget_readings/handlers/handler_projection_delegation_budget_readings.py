# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure, deterministic fold for one delegate-skill terminal (OMN-21326 AC3).

Canonical definition B (CLAUDE.md rule 7a): typed request in, typed result out,
no I/O, no clock. The effect-class writer beside it reads the stored row, calls
this fold, and persists what it returns.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_delegation_budget_readings.models import (
    ModelDelegationBudgetReadingProjectionRequest,
    ModelDelegationBudgetReadingProjectionResult,
    ModelDelegationBudgetReadingRow,
)


class HandlerProjectionDelegationBudgetReadings:
    """One row per correlation id; a later terminal of the same id replaces it."""

    def handle(
        self, request: ModelDelegationBudgetReadingProjectionRequest
    ) -> ModelDelegationBudgetReadingProjectionResult:
        event = request.event
        previous = request.previous_row
        # A redelivery, or an older terminal of the same id arriving late, never
        # replaces a newer one. observed_at (the producer's envelope time)
        # orders terminals of one id.
        if previous is not None and event.observed_at <= previous.observed_at:
            return ModelDelegationBudgetReadingProjectionResult(
                row=previous, applied=False
            )
        budget = event.budget_evidence
        row = ModelDelegationBudgetReadingRow(
            correlation_id=event.correlation_id,
            status=event.status,
            task_type=event.task_type,
            terminal_topic=event.terminal_topic,
            requested_timeout_seconds=(
                budget.requested_timeout_seconds if budget is not None else None
            ),
            execution_timeout_seconds=(
                budget.execution_timeout_seconds if budget is not None else None
            ),
            execution_duration_ms=event.execution_duration_ms,
            queue_wait_ms=event.queue_wait_ms,
            caller_lane=event.caller_lane,
            ticket_id=event.ticket_id,
            observed_at=event.observed_at,
        )
        return ModelDelegationBudgetReadingProjectionResult(row=row, applied=True)


__all__ = ["HandlerProjectionDelegationBudgetReadings"]
