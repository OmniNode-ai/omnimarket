# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from omnimarket.nodes.node_build_loop_orchestrator.models import (
    ModelOrchestratorResult,
    ModelOrchestratorStartCommand,
)


def assemble_live_orchestrator(
    start_command: ModelOrchestratorStartCommand,
) -> ModelOrchestratorResult:
    """Assemble a live orchestrator result from a start command."""
    return ModelOrchestratorResult(
        correlation_id=start_command.correlation_id,
    )
