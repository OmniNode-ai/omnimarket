# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_session_phase_dispatcher."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from omnibase_core.models.overseer.model_session_phase_spec import ModelSessionPhaseSpec
from pydantic import BaseModel, ConfigDict, Field


class ModelSessionPhaseTransitionCommand(BaseModel):
    """A phase transition command consumed from onex.cmd.omnimarket.session-phase-transition.v1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    session_id: str  # string-id-ok: overseer correlation string
    phase_name: str
    transition: Literal["enter", "exit", "skip", "fail"]
    phase_spec: ModelSessionPhaseSpec | None = None
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    budget_usd: float = Field(default=5.0, gt=0.0)


class ModelSessionPhaseDispatcherInput(BaseModel):
    """Input envelope for HandlerSessionPhaseDispatcher."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commands: tuple[ModelSessionPhaseTransitionCommand, ...] = Field(
        ...,
        min_length=1,
        description="One or more phase transition commands to process.",
    )


__all__ = [
    "ModelSessionPhaseDispatcherInput",
    "ModelSessionPhaseTransitionCommand",
]
