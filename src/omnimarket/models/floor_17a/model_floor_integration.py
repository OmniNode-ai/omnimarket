# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's integration section (OMN-23899): merged work that is not running, oldest first, grouped by what it would take.

The floor reads the integration floor projection (published by node_integration_reading_publish_effect from the hourly
integration pass) and draws its counts and its unintegrated changes. A projection that was not read, or is older than
its own bound, draws the section unread with the reason, never zero. The words are the overlay's; an overlay that
declares no section draws it not configured.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.integration_reading import (
    EnumIntegrationGapClass,
    EnumIntegrationState,
    ModelIntegrationCounts,
    ModelIntegrationProjection,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelFloorIntegrationOverlay(_Frozen):
    """The words of the section: its title, one heading per gap class, and how many rows the floor draws."""

    title: str = Field(min_length=1)
    group_titles: dict[EnumIntegrationGapClass, str]
    max_rows: int = Field(gt=0)

    @model_validator(mode="after")
    def _every_class_titled(self) -> ModelFloorIntegrationOverlay:
        wanted = {
            c for c in EnumIntegrationGapClass if c is not EnumIntegrationGapClass.NONE
        }
        missing = sorted(c.value for c in wanted - set(self.group_titles))
        if missing:
            raise ValueError(
                f"the integration section titles every gap class; missing {missing}"
            )
        if EnumIntegrationGapClass.NONE in self.group_titles:
            raise ValueError(
                "the integration section titles no group for gap class none"
            )
        return self


class ModelFloorIntegrationIn(_Frozen):
    """The projection as this cycle read it: ``read`` with the projection, or ``unread`` with the reason."""

    status: Literal["read", "unread"]
    reason: str = ""
    as_of: datetime | None = None
    age_s: int | None = None
    projection: ModelIntegrationProjection | None = None

    @model_validator(mode="after")
    def _shape(self) -> ModelFloorIntegrationIn:
        if self.status == "read" and self.projection is None:
            raise ValueError("a read integration projection carries the projection")
        if self.status == "unread" and (self.projection is not None or not self.reason):
            raise ValueError(
                "an unread integration projection carries its reason and no projection"
            )
        return self


class ModelFloorIntegrationRow(_Frozen):
    """One unintegrated change as the floor draws it."""

    key: str
    title: str
    merged_at: datetime
    target: str
    state: EnumIntegrationState
    gap_class: EnumIntegrationGapClass
    gap: str
    step: str
    owner: str
    companions: tuple[str, ...] = ()


class ModelFloorIntegrationGroupLine(_Frozen):
    """The unintegrated changes that would take the same kind of step, oldest merge first."""

    gap_class: EnumIntegrationGapClass
    title: str
    count: int = Field(ge=0)
    keys: tuple[str, ...]


class ModelFloorIntegrationSection(_Frozen):
    """The section: the counts, the groups and the not-running rows oldest first (at most the overlay's max_rows)."""

    status: Literal["read", "unread", "not_configured"]
    title: str = ""
    reason: str = ""
    as_of: datetime | None = None
    age_s: int | None = None
    counts: ModelIntegrationCounts | None = None
    groups: tuple[ModelFloorIntegrationGroupLine, ...] = ()
    rows: tuple[ModelFloorIntegrationRow, ...] = ()
    rows_total: int = Field(default=0, ge=0)


__all__ = [
    "ModelFloorIntegrationGroupLine",
    "ModelFloorIntegrationIn",
    "ModelFloorIntegrationOverlay",
    "ModelFloorIntegrationRow",
    "ModelFloorIntegrationSection",
]
