# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus RPC boundary; the orchestrator never starts a model process."""

from typing import Protocol

from pydantic import JsonValue

from ..models.model_morning_friction_sweep import (
    ModelFrictionPhaseRequest,
    ModelMorningFrictionSweepRequest,
)


class ProtocolFrictionPhaseGateway(Protocol):
    async def run(
        self, run: ModelMorningFrictionSweepRequest, phase: ModelFrictionPhaseRequest
    ) -> dict[str, JsonValue] | None: ...
