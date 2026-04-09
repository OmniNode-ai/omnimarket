# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from omnimarket.nodes.node_build_loop_orchestrator.models import (
    ModelDispatchMetrics,
    ModelDispatchTrace,
    ModelLiveRunnerConfig,
    ModelLoopCycleSummary,
    ModelOrchestratorResult,
    ModelOrchestratorStartCommand,
)


def assemble_live_orchestrator(
    start_command: ModelOrchestratorStartCommand,
    config: ModelLiveRunnerConfig,
) -> ModelOrchestratorResult:
    # Logic for assembling live orchestrator
    return ModelOrchestratorResult(
        cycle_summary=ModelLoopCycleSummary(),
        metrics=ModelDispatchMetrics(),
        trace=ModelDispatchTrace(),
    )
