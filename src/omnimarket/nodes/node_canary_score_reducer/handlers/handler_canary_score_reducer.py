# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerCanaryScoreReducer -- composite scoring for canary model reports.

Consumes ModelCanaryReport events and accumulates weighted composite scores
into ModelScoreReducerState for materialization to capability_scores and
routing_outcomes tables.

[OMN-10845] [OMN-10847]
"""

from __future__ import annotations

from omnimarket.events.canary import ModelCanaryReport
from omnimarket.nodes.node_canary_score_reducer.models.model_materialize_result import (
    ModelMaterializeResult,
)
from omnimarket.nodes.node_canary_score_reducer.models.model_score_reducer_state import (
    ModelCapabilityScoreRow,
    ModelScoreReducerState,
)

TASK_TYPE = "adr_extraction"

WEIGHT_RECALL = 0.35
WEIGHT_PRECISION = 0.35
WEIGHT_FIDELITY = 0.20
WEIGHT_FORMAT = 0.10


class HandlerCanaryScoreReducer:
    """Accumulates canary reports into a score reducer state and materializes rows."""

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
        """Produce rows for capability_scores and routing_outcomes tables."""
        capability_score_rows: list[dict[str, object]] = []
        routing_outcome_rows: list[dict[str, object]] = []

        scored_rows = list(state.scores.values())

        # Determine which model has the highest composite score (selected=True)
        best_key: str | None = None
        best_score: float = -1.0
        for row in scored_rows:
            if row.composite_score is not None and row.composite_score > best_score:
                best_score = row.composite_score
                best_key = f"{row.model_key}::{row.task_type}"

        for row in scored_rows:
            success_count = max(row.entries_evaluated - row.entries_failed, 0)
            capability_score_rows.append(
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
            routing_outcome_rows.append(
                {
                    "correlation_id": row.canary_run_id,
                    "model_key": row.model_key,
                    "task_type": row.task_type,
                    "selected": (f"{row.model_key}::{row.task_type}" == best_key),
                    "quality_score": row.composite_score,
                    "actual_latency_ms": float(row.total_latency_ms)
                    / max(row.entries_evaluated, 1),
                    "actual_cost": row.estimated_cost_usd,
                }
            )

        return ModelMaterializeResult(
            capability_score_rows=capability_score_rows,
            routing_outcome_rows=routing_outcome_rows,
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
