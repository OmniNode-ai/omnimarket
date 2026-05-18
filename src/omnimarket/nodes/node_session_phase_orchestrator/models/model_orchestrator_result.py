# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for node_session_phase_orchestrator."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelSessionPhaseCommand(BaseModel):
    """A command emitted by the orchestrator to the event bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    command_type: str
    payload: dict[str, object]


class ModelSessionPhaseOrchestratorResult(BaseModel):
    """Result of one orchestrator handle() call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    commands: tuple[ModelSessionPhaseCommand, ...] = Field(
        default=(), description="Commands emitted to the event bus."
    )
    tick_outcome: Literal[
        "no_action", "transition_dispatched", "halt_dispatched", "warning_delegated"
    ]


__all__ = [
    "ModelSessionPhaseCommand",
    "ModelSessionPhaseOrchestratorResult",
]
