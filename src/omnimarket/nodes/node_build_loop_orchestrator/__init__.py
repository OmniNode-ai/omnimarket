# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build Loop Orchestrator Node

Orchestrates build loop execution with configurable phases,
dispatching, and comprehensive lifecycle management.

Components:
- OrchestratorStartCommand: Initiates build loop
- PhaseCommandIntent: Manages build phases
- LiveRunnerConfig: Build environment configuration
- OrchestratorCompletedEvent: Completion signaling
- DispatchMetrics: Execution metrics
- DispatchTrace: Execution tracing
- OrchestratorState: Orchestrator status
- LoopCycleSummary: Cycle summary
"""
