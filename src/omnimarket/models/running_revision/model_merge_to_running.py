# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One merge's time to reach a running build."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelMergeToRunning(BaseModel):
    """``contained=False`` means no running build contains it yet; ``seconds`` is its age at ``observed_at``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merged_at: datetime
    running_since: datetime | None
    seconds: int = Field(ge=0)
    contained: bool

    @field_validator("merged_at", "running_since")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("a merge time must carry a UTC offset")
        return value
