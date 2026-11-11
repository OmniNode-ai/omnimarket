# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev_tip_changed event: dev's tip moved to a new sha in one repository."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelDevTipChanged(BaseModel):
    """Published by the merged-change CI job where it now sends a roll request.

    Topic ``onex.evt.omnimarket.dev-tip-changed.v1``. The CI step is not changed
    in the shadow build; this is the contract it will publish to (plan R4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    repository: str = Field(min_length=1)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merged_at: datetime

    @field_validator("merged_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("merged_at must carry a UTC offset")
        return value
