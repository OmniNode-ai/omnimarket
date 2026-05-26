# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler stub for node_wave_scheduler_orchestrator [OMN-12210].

ORCHESTRATOR node. Consumes ModelWaveSchedulerRequest, parses the plan file,
builds a dependency DAG, computes execution waves with configurable max
concurrency, and dispatches parallel ticket-pipeline workers per wave.

Implementation is deferred (node_not_implemented: true). This stub raises
NotImplementedError so the runtime fails loudly rather than silently misbehaving.
"""

from __future__ import annotations

from omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_request import (
    ModelWaveSchedulerRequest,
)
from omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_result import (
    ModelWaveSchedulerResult,
)


class HandlerWaveSchedulerOrchestrator:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelWaveSchedulerRequest) -> ModelWaveSchedulerResult:
        raise NotImplementedError(  # stub-ok
            "node_wave_scheduler_orchestrator is not yet implemented (OMN-12210). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
