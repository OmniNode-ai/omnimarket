# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure compute handler for swarm result aggregation.

Concatenation mode: orders subtask outputs by wave/subtask_id and concatenates.
Synthesis mode: uses pre-fetched synthesis_output directly — never calls an LLM.

Accepts two request shapes (see ``ModelSwarmAggregateRequest`` docstring):
1. Typed ``decomposition`` + ``dispatches`` — direct / test callers.
2. ``subtasks`` + ``dispatches_json`` — orchestrator.
"""

from __future__ import annotations

import json as json_mod
import logging

from omnimarket.nodes.node_swarm_aggregator_compute.models.enums import (
    EnumAggregationMode,
    EnumSubtaskStatus,
)
from omnimarket.nodes.node_swarm_aggregator_compute.models.model_subtask import (
    ModelSubtask,
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

logger = logging.getLogger(__name__)

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
    # Resolve helpers for dual-shape request
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_subtasks(
        request: ModelSwarmAggregateRequest,
    ) -> tuple[ModelSubtask, ...]:
        """Return subtask ordering — from decomposition or top-level subtasks."""
        if request.decomposition is not None:
            return request.decomposition.subtasks
        return request.subtasks

    @staticmethod
    def _resolve_dispatches(
        request: ModelSwarmAggregateRequest,
    ) -> tuple[ModelSwarmDispatch, ...]:
        """Return typed dispatches — from field or parsed from dispatches_json."""
        if request.dispatches:
            return request.dispatches
        if request.dispatches_json:
            try:
                raw = json_mod.loads(request.dispatches_json)
                if isinstance(raw, list):
                    return tuple(ModelSwarmDispatch.model_validate(d) for d in raw)
            except Exception as exc:
                logger.warning("Failed to parse dispatches_json: %s", exc)
        return ()

    # ------------------------------------------------------------------
    # Concatenation
    # ------------------------------------------------------------------

    def _concatenation(
        self, request: ModelSwarmAggregateRequest
    ) -> ModelSwarmAggregateResult:
        subtasks = self._resolve_subtasks(request)
        dispatches = self._resolve_dispatches(request)

        subtask_order = {s.subtask_id: i for i, s in enumerate(subtasks)}
        sorted_dispatches = sorted(
            dispatches,
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
            run_id=request.run_id,
        )

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    def _synthesis(
        self, request: ModelSwarmAggregateRequest
    ) -> ModelSwarmAggregateResult:
        dispatches = self._resolve_dispatches(request)
        failed: list[str] = []
        skipped: list[str] = []
        for dispatch in dispatches:
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
            run_id=request.run_id,
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
