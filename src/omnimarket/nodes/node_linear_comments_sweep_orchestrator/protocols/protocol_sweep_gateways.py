# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus boundaries of the sweep; the orchestrator never starts a model process or decides alone."""

from typing import Protocol

from pydantic import JsonValue

from ..models.model_linear_comments_sweep import (
    ModelLinearCommentsSweepRequest,
    ModelSweepPhaseReply,
    ModelSweepPhaseRequest,
)


class ProtocolSweepPhaseGateway(Protocol):
    """One model call through the canonical delegation request/reply wiring."""

    async def run(
        self, run: ModelLinearCommentsSweepRequest, phase: ModelSweepPhaseRequest
    ) -> ModelSweepPhaseReply: ...


class ProtocolSweepDecisionGateway(Protocol):
    """One deterministic decision, asked of the Linear-comments decision compute node over the bus."""

    async def decide(
        self, run: ModelLinearCommentsSweepRequest, request: dict[str, JsonValue]
    ) -> dict[str, JsonValue]: ...
