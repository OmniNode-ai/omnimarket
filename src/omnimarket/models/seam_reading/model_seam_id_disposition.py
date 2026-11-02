# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One id that crossed a seam, what its side did with it, and the side's own time for it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.seam_reading.enum_seam_cause import EnumSeamCause
from omnimarket.models.seam_reading.enum_seam_disposition import EnumSeamDisposition


class ModelSeamIdDisposition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    disposition: EnumSeamDisposition
    cause: EnumSeamCause | None = None
    stamp: datetime | None = Field(
        default=None,
        description="sent_at on the sender, received_at on the receiver, from the emitting effect's injected clock.",
    )

    @model_validator(mode="after")
    def _cause_belongs_to_blocked_refused_or_stale(self) -> ModelSeamIdDisposition:
        needs = self.disposition in (
            EnumSeamDisposition.BLOCKED,
            EnumSeamDisposition.REFUSED,
        )
        if needs and self.cause is None:
            raise ValueError(f"a {self.disposition.value} id carries its cause")
        return self
