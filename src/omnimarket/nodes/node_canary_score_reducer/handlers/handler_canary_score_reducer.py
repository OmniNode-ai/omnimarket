# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerCanaryScoreReducer -- composite scoring for canary model reports.

Consumes ModelCanaryReport events and accumulates weighted composite scores
into ModelScoreReducerState for materialization to capability_scores table.

[OMN-10845]
"""

from __future__ import annotations

from omnimarket.events.canary import ModelCanaryReport
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

    def materialize(self, state: ModelScoreReducerState) -> list[dict[str, object]]:
        """Produce rows matching the capability_scores table schema."""
        rows: list[dict[str, object]] = []
        for row in state.scores.values():
            success_count = max(row.entries_evaluated - row.entries_failed, 0)
            rows.append(
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
        return rows

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
