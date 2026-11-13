# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One first-parent merge into dev: its sha and when it merged."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelDevMerge(BaseModel):
    """A merge commit on dev's first-parent chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merged_at: datetime

    @field_validator("merged_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("merged_at must carry a UTC offset")
        return value
