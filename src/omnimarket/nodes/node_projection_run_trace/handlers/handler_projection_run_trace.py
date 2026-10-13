# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold: one numbered step event in, its row out.

The row's place in the run is the step sequence the orchestrator assigned. The
fold reads no clock and keeps no state, so the same event always gives the same
row whatever arrived before it.
"""

from datetime import UTC

from omnimarket.nodes.node_projection_run_trace.models import (
    ModelDelegationRunStep,
    ModelRunTraceRow,
)


class HandlerProjectionRunTrace:
    def handle(self, event: ModelDelegationRunStep) -> ModelRunTraceRow:
        return ModelRunTraceRow(
            tenant_id=event.tenant_id,
            correlation_id=str(event.correlation_id),
            step_sequence=event.step_sequence,
            step_kind=event.step_kind,
            started_at=event.started_at.astimezone(UTC),
            duration_ms=event.duration_ms,
            outcome=event.outcome,
            failure_cause=event.failure_cause,
            backend_id=event.backend_id,
            model_id=event.model_id,
            attempt_number=event.attempt_number,
            reason=event.reason,
            candidates=event.candidates,
            failing_step_sequence=event.failing_step_sequence,
        )


__all__ = ["HandlerProjectionRunTrace"]
