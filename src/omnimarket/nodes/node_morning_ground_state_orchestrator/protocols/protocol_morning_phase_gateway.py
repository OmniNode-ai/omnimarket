# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus RPC boundary; the orchestrator never starts a model process."""

from typing import Protocol

from pydantic import JsonValue

from ..models.model_morning_ground_state import (
    ModelMorningGroundStateRequest,
    ModelMorningPhaseRequest,
)


class ProtocolMorningPhaseGateway(Protocol):
    async def run(
        self, run: ModelMorningGroundStateRequest, phase: ModelMorningPhaseRequest
    ) -> dict[str, JsonValue] | None: ...
    async def reconcile(self, run: ModelMorningGroundStateRequest) -> None: ...
