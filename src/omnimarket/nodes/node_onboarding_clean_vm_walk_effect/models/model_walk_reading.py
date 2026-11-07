# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, overlay and result of the weekly clean-VM walk reading."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    ModelWalkReceipt,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelWalkReadingOverlay(_FrozenModel):
    """The walk reading's settings, supplied by an overlay and never by a default."""

    max_age_seconds: int = Field(ge=1)


class ModelWalkReadingRequest(_FrozenModel):
    """Everything the reading is a function of; it reads no clock, file or network."""

    # None is a walk that has not run: the reading is UNKNOWN.
    receipt: ModelWalkReceipt | None
    now: datetime
    max_age_seconds: int = Field(ge=1)

    @field_validator("now")
    @classmethod
    def _now_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("now must carry a timezone")
        return value


class ModelWalkReading(_FrozenModel):
    verdict: EnumReadingVerdict
    # Token-safe reason codes in a fixed order; empty only for WORKING.
    reasons: tuple[str, ...]
    ends_in_delegated_result: bool
    probed_at: datetime
    published_at: datetime | None
    age_seconds: int | None
    max_age_seconds: int
