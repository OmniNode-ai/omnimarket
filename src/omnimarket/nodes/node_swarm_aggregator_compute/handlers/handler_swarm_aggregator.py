# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure compute handler for swarm result aggregation.

Concatenation mode: orders subtask outputs by wave/subtask_id and concatenates.
Synthesis mode: uses pre-fetched synthesis_output directly — never calls an LLM.
"""

from __future__ import annotations

from omnimarket.nodes.node_swarm_aggregator_compute.models.enums import (
    EnumAggregationMode,
    EnumSubtaskStatus,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_aggregate_request import (
    ModelSwarmAggregateRequest,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_aggregate_result import (
    ModelSwarmAggregateResult,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_swarm_dispatch import (
    ModelSwarmDispatch,
)

_FAILED_STATUSES = frozenset(
    {
        EnumSubtaskStatus.FAILED,
        EnumSubtaskStatus.TIMEOUT,
        EnumSubtaskStatus.CONTEXT_WINDOW_EXCEEDED,
    }
)

_SECTION_SEPARATOR = "\n\n---\n\n"


class HandlerSwarmAggregator:
    """Deterministic, pure compute aggregator. No I/O, no LLM calls."""

    def run(self, request: ModelSwarmAggregateRequest) -> ModelSwarmAggregateResult:
        if (
            request.mode == EnumAggregationMode.SYNTHESIS
            and request.synthesis_output is not None
        ):
            return self._synthesis(request)
        return self._concatenation(request)

    # ------------------------------------------------------------------
    # Concatenation
    # ------------------------------------------------------------------

    def _concatenation(
        self, request: ModelSwarmAggregateRequest
    ) -> ModelSwarmAggregateResult:
        subtask_order = {
            s.subtask_id: i for i, s in enumerate(request.decomposition.subtasks)
        }
        sorted_dispatches = sorted(
            request.dispatches,
            key=lambda d: (d.wave, subtask_order.get(d.subtask_id, 999), d.subtask_id),
        )

        sections: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []

        for dispatch in sorted_dispatches:
            if dispatch.status == EnumSubtaskStatus.SUCCEEDED:
                text = self._extract_text(dispatch)
                sections.append(f"## Subtask: {dispatch.subtask_id}\n\n{text}")
            elif dispatch.status == EnumSubtaskStatus.SKIPPED_DEPENDENCY_FAILED:
                skipped.append(dispatch.subtask_id)
            elif dispatch.status in _FAILED_STATUSES:
                failed.append(dispatch.subtask_id)

        aggregated = _SECTION_SEPARATOR.join(sections)
        degraded_reason = ""
        if failed:
            degraded_reason = f"Failed subtasks: {', '.join(failed)}"
            if skipped:
                degraded_reason += f"; Skipped subtasks: {', '.join(skipped)}"
        elif skipped:
            degraded_reason = f"Skipped subtasks: {', '.join(skipped)}"

        return ModelSwarmAggregateResult(
            aggregated_output=aggregated,
            aggregation_mode=EnumAggregationMode.CONCATENATION,
            failed_subtasks=tuple(failed),
            skipped_subtasks=tuple(skipped),
            degraded_reason=degraded_reason,
        )

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    def _synthesis(
        self, request: ModelSwarmAggregateRequest
    ) -> ModelSwarmAggregateResult:
        failed: list[str] = []
        skipped: list[str] = []
        for dispatch in request.dispatches:
            if dispatch.status == EnumSubtaskStatus.SKIPPED_DEPENDENCY_FAILED:
                skipped.append(dispatch.subtask_id)
            elif dispatch.status in _FAILED_STATUSES:
                failed.append(dispatch.subtask_id)

        return ModelSwarmAggregateResult(
            aggregated_output=request.synthesis_output or "",
            aggregation_mode=EnumAggregationMode.SYNTHESIS,
            failed_subtasks=tuple(failed),
            skipped_subtasks=tuple(skipped),
            synthesis_input_hash=request.synthesis_input_hash or "",
            synthesis_model_id=request.synthesis_model_id or "",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(dispatch: ModelSwarmDispatch) -> str:
        if dispatch.result is None:
            return ""
        r = dispatch.result
        return (
            r.response_text or r.handler_source or r.contract_yaml or r.task_description
        )
