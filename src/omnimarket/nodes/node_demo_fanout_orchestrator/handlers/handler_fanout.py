# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_demo_fanout_orchestrator [OMN-12235].

ORCHESTRATOR node. Accepts a list of tasks and model configs, fans out
inference requests to each model in parallel, and collects per-model
results with token counts and latency measurements.

node_not_implemented: true — raise NotImplementedError until Wave 7 implementation.
"""

from __future__ import annotations

from omnimarket.nodes.node_demo_fanout_orchestrator.models.model_fanout_request import (
    ModelDemoFanoutRequest,
    ModelDemoFanoutResult,
)


class HandlerDemoFanoutOrchestrator:
    """ORCHESTRATOR — fan-out LLM inference across multiple model configs."""

    async def handle(self, request: ModelDemoFanoutRequest) -> ModelDemoFanoutResult:
        raise NotImplementedError  # stub-ok
