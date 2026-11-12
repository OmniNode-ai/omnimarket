# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The decision: per-repository readings, merge times, median and red reasons."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from omnimarket.models.running_revision.enum_running_revision_red_reason import (
    EnumRunningRevisionRedReason,
)
from omnimarket.models.running_revision.enum_running_revision_status import (
    EnumRunningRevisionStatus,
)
from omnimarket.models.running_revision.model_merge_to_running import (
    ModelMergeToRunning,
)
from omnimarket.models.running_revision.model_oldest_not_running import (
    ModelOldestNotRunning,
)
from omnimarket.models.running_revision.model_repository_reading import (
    ModelRepositoryReading,
)


class ModelRunningRevisionReading(BaseModel):
    """The pure decision published on the decided topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    observed_at: datetime
    status: EnumRunningRevisionStatus
    repositories: tuple[ModelRepositoryReading, ...]
    merge_times: tuple[ModelMergeToRunning, ...]
    median_merge_to_running_seconds: int | None
    median_over_bound: bool
    oldest_not_running: ModelOldestNotRunning | None
    red: bool
    red_reasons: tuple[EnumRunningRevisionRedReason, ...]
    current_build_id: str | None
    current_source_hash_recorded: bool

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("observed_at must carry a UTC offset")
        return value

    @model_validator(mode="after")
    def _red_matches_reasons(self) -> ModelRunningRevisionReading:
        if self.red and not self.red_reasons:
            raise ValueError("red needs at least one red_reason")
        if not self.red and self.red_reasons:
            raise ValueError("red_reasons are set exactly when red is true")
        return self
