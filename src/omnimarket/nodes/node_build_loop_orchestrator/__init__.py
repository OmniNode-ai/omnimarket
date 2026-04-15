# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#!/usr/bin/env python3

"""
Orchestrates build loop processes.
Manages the execution flow of build loops, tracking state and dispatching commands.
"""

from .models import (
    ModelDispatchMetrics,
    ModelDispatchTrace,
    ModelLiveRunnerConfig,
    ModelLoopCycleSummary,
    ModelOrchestratorCompletedEvent,
    ModelOrchestratorResult,
    ModelOrchestratorStartCommand,
    ModelOrchestratorState,
    ModelPhaseCommandIntent,
)
from .protocols import (
    ProtocolBuildDispatchHandler,
    ProtocolCloseoutHandler,
    ProtocolRsdFillHandler,
    ProtocolTicketClassifyHandler,
    ProtocolVerifyHandler,
)

__all__ = [
    "ModelDispatchMetrics",
    "ModelDispatchTrace",
    "ModelLiveRunnerConfig",
    "ModelLoopCycleSummary",
    "ModelOrchestratorCompletedEvent",
    "ModelOrchestratorResult",
    "ModelOrchestratorStartCommand",
    "ModelOrchestratorState",
    "ModelPhaseCommandIntent",
    "NodeBuildLoopOrchestrator",
    "ProtocolBuildDispatchHandler",
    "ProtocolCloseoutHandler",
    "ProtocolRsdFillHandler",
    "ProtocolTicketClassifyHandler",
    "ProtocolVerifyHandler",
]
from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    HandlerBuildLoopOrchestrator,
)


class NodeBuildLoopOrchestrator(HandlerBuildLoopOrchestrator):
    """ONEX entry-point wrapper for HandlerBuildLoopOrchestrator."""
