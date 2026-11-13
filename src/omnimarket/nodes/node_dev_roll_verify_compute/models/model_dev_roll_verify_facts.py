# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One verdict's input: the roll's start events, finish time and installs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_dev_roll_verify_compute.models.model_dev_branch_install import (
    ModelDevBranchInstall,
)
from omnimarket.nodes.node_dev_roll_verify_compute.models.model_dev_runtime_started import (
    ModelDevRuntimeStarted,
)


class ModelDevRollVerifyFacts(BaseModel):
    """The compute reads no clock: ``observed_at`` is the snapshot time.

    ``starts`` is ``None`` when the start events could not be read; that facts
    record still gets the ``start_count_unknown`` verdict, not a refusal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    observed_at: datetime
    job_id: str = Field(min_length=1)
    roll_finished_at: datetime | None
    starts: tuple[ModelDevRuntimeStarted, ...] | None
    declared_branch_installs: tuple[ModelDevBranchInstall, ...]

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("observed_at must carry a UTC offset")
        return value

    @field_validator("roll_finished_at")
    @classmethod
    def _finished_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("roll_finished_at must carry a UTC offset")
        return value
