# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerCanaryScoreReducer -- composite scoring for canary model reports.

Consumes ModelCanaryReport events and accumulates weighted composite scores
into ModelScoreReducerState for materialization to capability_scores table.

[OMN-10845]
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from omnimarket.events.canary import ModelCanaryReport
from omnimarket.nodes.node_canary_score_reducer.models.model_score_reducer_state import (
    ModelCapabilityScoreRow,
    ModelMaterializeResult,
    ModelScoreReducerState,
)
from omnimarket.projection.protocol_database import DatabaseAdapter
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID

TASK_TYPE = "adr_extraction"
TABLE = "capability_scores"
CONFLICT_KEY = "model_key,task_type"

WEIGHT_RECALL = 0.35
WEIGHT_PRECISION = 0.35
WEIGHT_FIDELITY = 0.20
WEIGHT_FORMAT = 0.10


class HandlerCanaryScoreReducer:
    """Accumulates canary reports into a score reducer state and materializes rows."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Projection-arm entrypoint: fold one canary report and upsert its rows.

        The contract declares ``db_io.db_tables``, so auto-wiring dispatches by
        calling ``handle(input_data)`` with the runtime-injected adapter under
        ``_db``. Every other ``_``-prefixed key is runtime delivery metadata, not
        report content. The fold itself stays in ``accumulate``/``materialize``;
        this method only binds them to the table.

        Each event is folded from an empty state: the table is the durable
        state, and the upsert key ``(model_key, task_type)`` makes the newest
        successful canary run the row's value. Scores are platform-own, so rows
        belong to the house tenant, which the row must name for RLS to admit it.
        """
        payload = dict(input_data)
        db = payload.pop("_db", None)
        if not isinstance(db, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        report = ModelCanaryReport.model_validate(
            {k: v for k, v in payload.items() if not k.startswith("_")}
        )

        state = self.accumulate(ModelScoreReducerState(), report)
        now = datetime.now(UTC).isoformat()
        rows = self.materialize(state).capability_score_rows
        for row in rows:
            db.upsert(
                TABLE,
                CONFLICT_KEY,
                {
                    "model_key": row["model_key"],
                    "task_type": row["task_type"],
                    "success_count": row["success_count"],
                    "failure_count": row["failure_count"],
                    "total_count": row["total_count"],
                    "success_rate": row["success_rate"] or 0.0,
                    # avg_latency_ms is an INT column.
                    "avg_latency_ms": round(cast("float", row["avg_latency_ms"])),
                    "total_cost": row["total_cost"] or 0.0,
                    "last_updated": now,
                    "tenant_id": str(HOUSE_TENANT_UUID),
                },
            )
        return {"rows_upserted": len(rows)}

    def accumulate(
        self,
        state: ModelScoreReducerState,
        report: ModelCanaryReport,
    ) -> ModelScoreReducerState:
        """Merge a canary report into the existing state.

        If the report is not successful, the state is returned unchanged.
        """
        if not report.success:
            return state

        new_scores = dict(state.scores)
        for ms in report.model_scores:
            key = f"{ms.model_key}::{TASK_TYPE}"
            composite = self.compute_composite(
                recall=ms.avg_recall,
                precision=ms.avg_precision,
                fidelity=ms.avg_fidelity,
                format_compliance=ms.avg_format_compliance,
            )
            new_scores[key] = ModelCapabilityScoreRow(
                model_key=ms.model_key,
                task_type=TASK_TYPE,
                avg_recall=ms.avg_recall,
                avg_precision=ms.avg_precision,
                avg_fidelity=ms.avg_fidelity,
                avg_format_compliance=ms.avg_format_compliance,
                composite_score=composite,
                entries_evaluated=ms.entries_evaluated,
                entries_failed=ms.entries_failed,
                estimated_cost_usd=ms.estimated_cost_usd,
                total_latency_ms=ms.total_latency_ms,
                canary_run_id=report.run_id,
            )
        return ModelScoreReducerState(scores=new_scores)

    def materialize(self, state: ModelScoreReducerState) -> ModelMaterializeResult:
        """Produce typed rows for capability_scores and routing_outcomes tables."""
        capability_rows: list[dict[str, object]] = []
        routing_rows: list[dict[str, object]] = []
        for row in state.scores.values():
            success_count = max(row.entries_evaluated - row.entries_failed, 0)
            capability_rows.append(
                {
                    "model_key": row.model_key,
                    "task_type": row.task_type,
                    "success_rate": row.composite_score,
                    "avg_latency_ms": float(row.total_latency_ms)
                    / max(row.entries_evaluated, 1),
                    "total_cost": row.estimated_cost_usd,
                    "total_count": row.entries_evaluated,
                    "success_count": success_count,
                    "failure_count": row.entries_failed,
                }
            )
            routing_rows.append(
                {
                    "model_key": row.model_key,
                    "task_type": row.task_type,
                    "quality_score": row.composite_score,
                    "canary_run_id": row.canary_run_id,
                }
            )
        return ModelMaterializeResult(
            capability_score_rows=tuple(capability_rows),
            routing_outcome_rows=tuple(routing_rows),
        )

    def compute_composite(
        self,
        recall: float | None,
        precision: float | None,
        fidelity: float | None,
        format_compliance: float | None,
    ) -> float | None:
        """Compute weighted composite score, ignoring None components."""
        components = [
            (recall, WEIGHT_RECALL),
            (precision, WEIGHT_PRECISION),
            (fidelity, WEIGHT_FIDELITY),
            (format_compliance, WEIGHT_FORMAT),
        ]
        scored = [(v, w) for v, w in components if v is not None]
        if not scored:
            return None
        total_weight = sum(w for _, w in scored)
        return sum(v * w for v, w in scored) / total_weight


__all__: list[str] = ["HandlerCanaryScoreReducer"]
