# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store-neutral write of one run step, keyed by the orchestrator's sequence."""

from datetime import UTC, datetime

from omnimarket.nodes.node_projection_run_trace.models import ModelRunTraceRow
from omnimarket.projection.protocol_database import DatabaseAdapter

RUN_TRACE_TABLE = "run_trace"
RUN_TRACE_KEY = "tenant_id,correlation_id,step_sequence"


def apply_run_step(row: ModelRunTraceRow, db: DatabaseAdapter) -> bool:
    """Store a step once; a step already stored under its key is left as it is.

    A step is a fact the orchestrator stated once. A redelivery, identical or
    not, performs no write, so a replay changes neither the row nor its time.
    """
    key: dict[str, object] = {
        "tenant_id": row.tenant_id,
        "correlation_id": row.correlation_id,
        "step_sequence": row.step_sequence,
    }
    if db.query(RUN_TRACE_TABLE, key):
        return False
    db.upsert(
        RUN_TRACE_TABLE,
        RUN_TRACE_KEY,
        {
            **key,
            "step_kind": row.step_kind.value,
            "started_at": row.started_at.isoformat(),
            "duration_ms": row.duration_ms,
            "outcome": row.outcome.value,
            "failure_cause": row.failure_cause,
            "backend_id": row.backend_id,
            "model_id": row.model_id,
            "attempt_number": row.attempt_number,
            "reason": row.reason,
            "candidates": list(row.candidates),
            "failing_step_sequence": row.failing_step_sequence,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    return True


__all__ = ["RUN_TRACE_KEY", "RUN_TRACE_TABLE", "apply_run_step"]
