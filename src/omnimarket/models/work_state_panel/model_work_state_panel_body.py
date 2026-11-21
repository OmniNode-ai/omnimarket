# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The panel body: one projection's counts and freshness, digested so a reader can pin it."""

from __future__ import annotations

import datetime
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.work_state_projection.enum_work_item_state import (
    EnumWorkItemState,
)
from omnimarket.models.work_state_projection.enum_work_state_source import (
    EnumWorkStateSource,
)
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    EnumWorkStateProjectorProfile,
)


class ModelWorkStatePanelBody(BaseModel):
    """What one projection run reported to the panel: the counts, the ready count and every source not fresh.

    The datetimes must be aware and are stored as UTC, so one instant digests the same at any offset.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime.datetime
    computed_at: datetime.datetime
    projector: EnumWorkStateProjectorProfile
    item_count: int = Field(ge=0)
    counts: dict[EnumWorkItemState, int]
    ready_in_scope: int = Field(ge=0)
    unread: tuple[EnumWorkStateSource, ...] = ()
    stale_sources: tuple[EnumWorkStateSource, ...] = ()

    @field_validator("as_of", "computed_at")
    @classmethod
    def _datetimes_are_aware(cls, value: datetime.datetime) -> datetime.datetime:
        if value.tzinfo is None:
            raise ValueError("as_of and computed_at must be aware datetimes")
        return value.astimezone(datetime.UTC)

    @field_validator("counts")
    @classmethod
    def _counts_are_non_negative(
        cls,
        counts: dict[EnumWorkItemState, int],
    ) -> dict[EnumWorkItemState, int]:
        for state, count in counts.items():
            if count < 0:
                raise ValueError(f"counts[{state.value}] must be >= 0")
        return counts

    def digest(self) -> str:
        """The sha256 of the body's canonical JSON; the counts' keys serialize to their values."""
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


__all__ = ["ModelWorkStatePanelBody"]
