# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The step reply a caller waits for: its own correlation id and the reply (OMN-26721)."""

from __future__ import annotations

from uuid import UUID

from omnibase_core.models.lane_work import ModelLaneWorkStepReply
from pydantic import BaseModel, ConfigDict


class ModelLaneWorkStepCompleted(BaseModel):
    """Published on the contract's ``terminal_event`` after every fold of a caller's input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    reply: ModelLaneWorkStepReply


__all__: list[str] = ["ModelLaneWorkStepCompleted"]
