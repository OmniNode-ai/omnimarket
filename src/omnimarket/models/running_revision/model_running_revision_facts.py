# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One reading's input: the build history and each repository's merge history."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.models.running_revision.model_repository_merge_history import (
    ModelRepositoryMergeHistory,
)
from omnimarket.models.running_revision.model_running_build import (
    ModelRunningBuild,
)


class ModelRunningRevisionFacts(BaseModel):
    """The compute reads no clock: ``observed_at`` is the snapshot's time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    observed_at: datetime
    builds: tuple[ModelRunningBuild, ...]
    histories: tuple[ModelRepositoryMergeHistory, ...]

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("observed_at must carry a UTC offset")
        return value
