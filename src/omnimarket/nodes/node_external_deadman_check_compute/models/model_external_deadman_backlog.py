# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Episodes a leg has not yet delivered or not yet recorded (OMN-20808)."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ModelExternalDeadmanBacklog(BaseModel):
    """The count and the oldest delivery-due time; a non-empty backlog must say how old it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int = Field(ge=0)
    oldest_due_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _non_empty_backlog_has_a_due_time(self) -> ModelExternalDeadmanBacklog:
        if self.count > 0 and self.oldest_due_at is None:
            raise ValueError("a backlog with episodes must carry oldest_due_at")
        return self
