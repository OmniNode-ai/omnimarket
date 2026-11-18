# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The oldest merge not yet in a running build, with its age in seconds."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.running_revision.model_dev_merge import (
    ModelDevMerge,
)


class ModelOldestNotRunning(BaseModel):
    """The merge the reading surfaces as the oldest not yet running."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    merge: ModelDevMerge
    age_seconds: int = Field(ge=0)
