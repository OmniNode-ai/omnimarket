# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionContextRoi — project context-ROI runner output to DB.

Consumes BOTH runner terminal events —
``onex.evt.omnimarket.context-roi-run-completed.v1`` AND
``onex.evt.omnimarket.context-roi-run-failed.v1`` — whose payload is the same
:class:`ModelContextRoiRunResult` carrying one
:class:`ModelAttemptReductionRow` per (task x arm x trial) cell. Each row is
upserted into the ``context_roi_scores`` table by its runner-minted
``correlation_id``. The table backs the projection-API topic
``onex.snapshot.projection.context.experiment-scores.v1`` consumed by the
omnidash /experiments panels.

The handler is terminal-agnostic by construction: the failure outcome is
already encoded on each row (``final_success`` / ``failure_stage``), so a
fully-failed run delivered on the failed terminal materialises rows with
``final_success=False`` through the exact same projection path. Subscribing to
the failed terminal (contract.yaml) is what keeps a failed run from wedging the
N-arm experiment battery with zero usable rows (OMN-13645). Mirrors
node_projection_delegation, which likewise folds both the completed and failed
delegation/generation terminals through one operation_match handler.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.context_roi import (
    ModelAttemptReductionRow,
    ModelContextRoiRunResult,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "context_roi_scores"
CONFLICT_KEY = "correlation_id"


class ModelContextRoiRunCompletedEvent(BaseModel):
    """Inbound event from onex.evt.omnimarket.context-roi-run-completed.v1.

    Payload is the runner terminal result: a run identifier plus the captured
    per-(task x arm x trial) attempt-reduction rows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, description="Stable run identifier.")
    rows: tuple[ModelAttemptReductionRow, ...] = Field(
        description="Per-(task x arm x trial) attempt-reduction rows."
    )


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionContextRoi:
    """Project context-ROI runner output into the context_roi_scores table."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Deserialises the runner terminal result from ``input_data`` and upserts
        one row per attempt-reduction row using a ``DatabaseAdapter`` supplied
        in ``input_data['_db']``.
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")

        event_data = {
            key: value
            for key, value in payload.items()
            if not key.startswith("_")
            and key not in {"event_landed", "latency_ms", "proof_class"}
        }
        result = ModelContextRoiRunResult(**event_data)
        projection = self.project(result, db_raw)
        return projection.model_dump(mode="json")

    def project(
        self,
        result: ModelContextRoiRunResult,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT one row per attempt-reduction row in the run result."""
        now = datetime.now(tz=UTC).isoformat()
        upserted = 0
        for row in result.rows:
            db_row: dict[str, object] = {
                "run_id": row.run_id or result.run_id,
                "correlation_id": row.correlation_id,
                "task_id": row.task_id,
                "run_order": row.run_order,
                "context_factor_subset": row.context_factor_subset,
                "context_pack_hash": row.context_pack_hash,
                "attempt_count": row.attempt_count,
                "first_pass_success": row.first_pass_success,
                "final_success": row.final_success,
                "failure_stage": str(row.failure_stage),
                "prompt_tokens": row.prompt_tokens,
                "completion_tokens": row.completion_tokens,
                "tokens_used": row.prompt_tokens + row.completion_tokens,
                "estimated_cost": row.estimated_cost,
                "model_id": row.model_id,
                "provider": row.provider,
                "endpoint_ref": row.endpoint_ref,
                "proof_class": str(row.proof_class),
                "created_at": now,
                "updated_at": now,
            }
            if db.upsert(TABLE, CONFLICT_KEY, db_row):
                upserted += 1
        return ModelProjectionResult(rows_upserted=upserted)


__all__ = [
    "HandlerProjectionContextRoi",
    "ModelContextRoiRunCompletedEvent",
    "ModelProjectionResult",
]
