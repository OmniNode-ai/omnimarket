# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Paired CLAUDE.md / AGENTS.md on-off benchmark node (OMN-21896)."""

from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.handler_context_benchmark import (
    HandlerContextBenchmark,
)


class NodeContextBenchmarkOrchestrator(HandlerContextBenchmark):
    """ONEX entrypoint for the context benchmark."""


__all__ = ["HandlerContextBenchmark", "NodeContextBenchmarkOrchestrator"]
