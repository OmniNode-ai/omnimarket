# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev_runtime_started event: one dev runtime start on one roll job."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_dev_roll_verify_compute.models.enum_dev_start_cause import (
    EnumDevStartCause,
)


class ModelDevRuntimeStarted(BaseModel):
    """Published by the deploy agent at each dev runtime start.

    Topic ``onex.evt.omnimarket.dev-runtime-started.v1`` (five whys plan R1).
    No producer exists yet; this model is the contract R1 must satisfy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    job_id: str = Field(min_length=1)
    runtime_lane: Literal["dev"]
    started_at: datetime
    cause: EnumDevStartCause

    @field_validator("started_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("started_at must carry a UTC offset")
        return value
