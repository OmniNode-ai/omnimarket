# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_sweep_outcome_classify [OMN-8963].

COMPUTE node. Pure function: completion event → outcome classification.
No side effects, no env reads, no bus publishes, no I/O.

6-branch classification table:
- event_type="armed":           armed=True  → ARMED  | armed=False → FAILED
- event_type="rebase_completed": success=True → REBASED
                                  success=False + conflict_files → STUCK
                                  success=False + no conflicts → FAILED
- event_type="ci_rerun_triggered": rerun_triggered=True → CI_RERUN_TRIGGERED
                                    rerun_triggered=False → FAILED
- unknown event_type → STUCK (safe fallback)
"""

from __future__ import annotations

import logging
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
    ModelSweepOutcomeClassified,
    ModelSweepOutcomeInput,
)

_log = logging.getLogger(__name__)


class HandlerSweepOutcomeClassify:
    """COMPUTE: classify completion events → outcome enum. Fully pure."""

    def handle(self, request: ModelSweepOutcomeInput) -> ModelHandlerOutput:  # type: ignore[type-arg]
        """Classify the completion event into an outcome."""
        outcome, error, conflict_files = self._classify(request)

        classified = ModelSweepOutcomeClassified(
            pr_number=request.pr_number,
            repo=request.repo,
            correlation_id=request.correlation_id,
            run_id=request.run_id,
            total_prs=request.total_prs,
            outcome=outcome,
            source_event_type=request.event_type,
            error=error,
            conflict_files=conflict_files,
        )
        return ModelHandlerOutput.for_compute(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_sweep_outcome_classify",
            result=classified,
        )

    def _classify(
        self, req: ModelSweepOutcomeInput
    ) -> tuple[EnumSweepOutcome, str | None, list[str]]:
        """Pure classification. Returns (outcome, error, conflict_files)."""
        if req.event_type == "armed":
            if req.armed is True:
                return EnumSweepOutcome.ARMED, None, []
            return EnumSweepOutcome.FAILED, req.error, []

        if req.event_type == "rebase_completed":
            if req.success is True:
                return EnumSweepOutcome.REBASED, None, []
            if req.conflict_files:
                return EnumSweepOutcome.STUCK, req.error, req.conflict_files
            return EnumSweepOutcome.FAILED, req.error, []

        if req.event_type == "ci_rerun_triggered":
            if req.rerun_triggered is True:
                return EnumSweepOutcome.CI_RERUN_TRIGGERED, None, []
            return EnumSweepOutcome.FAILED, req.error, []

        if req.event_type == "merged":
            return EnumSweepOutcome.MERGED, None, []

        # Unknown event type — safe fallback
        _log.warning(
            "Unknown event_type=%r for PR %s/%s; classifying as STUCK",
            req.event_type,
            req.repo,
            req.pr_number,
        )
        return EnumSweepOutcome.STUCK, f"unknown_event_type:{req.event_type}", []
