# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lease fences carried by repository write commands (OMN-21425)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumRepoWriteFenceStatus(StrEnum):
    """The fence port's answer, read as is: the same values as
    omnibase_infra's EnumLeaseFenceStatus (OMN-21424).
    """

    OK = "ok"
    STALE = "stale"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class ModelRepoWriteLease(BaseModel):
    """The lease a command acting on a leased resource carries (OMN-21425)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    fence: int = Field(gt=0, description="The etcd revision of the grant.")
    expires_at: datetime

    @model_validator(mode="after")
    def _expiry_has_timezone(self) -> ModelRepoWriteLease:
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must have a timezone")
        return self


__all__: list[str] = ["EnumRepoWriteFenceStatus", "ModelRepoWriteLease"]
