# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The step reply a caller waits for: its own correlation id and the reply (OMN-26721)."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from omnibase_core.models.lane_work import ModelLaneWorkStepReply
from pydantic import BaseModel, ConfigDict, model_validator


class ModelLaneWorkStepCompleted(BaseModel):
    """Published on the contract's ``terminal_event`` after every fold of a caller's input.

    A ``start`` the orchestrator refuses begins no workflow, so there is no step reply to
    carry: the answer is the refusal, with the caller's correlation id (exactly one of the two).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    reply: ModelLaneWorkStepReply | None = None
    refusal: str | None = None

    @model_validator(mode="after")
    def _one_answer(self) -> Self:
        if (self.reply is None) == (self.refusal is None):
            raise ValueError("a step answer carries exactly one of reply and refusal")
        return self


__all__: list[str] = ["ModelLaneWorkStepCompleted"]
