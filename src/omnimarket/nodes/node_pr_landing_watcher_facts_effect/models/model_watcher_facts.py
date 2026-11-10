# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, events and result of the landing watcher facts node (OMN-23507)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import ModelLandingFacts


class EnumWatcherFactsStatus(StrEnum):
    WRITTEN = "written"
    FAILED = "failed"


class EnumWatcherFactsFailureReason(StrEnum):
    STATE_UNREADABLE = "state_unreadable"
    STATE_STALE = "state_stale"
    FACTS_UNWRITABLE = "facts_unwritable"


class ModelWatcherFactsRequest(BaseModel):
    """Write one pass of landing facts from the watcher's state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID


class ModelWatcherFactsWritten(BaseModel):
    """The written event: the facts file now holds this tick for these repositories."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tick: int = Field(ge=0)
    prs: int = Field(ge=0, description="Open pull requests written.")
    repositories: tuple[str, ...]
    facts_path: str = Field(min_length=1)


class ModelWatcherFactsFailed(BaseModel):
    """The failed event: nothing was written."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    reason: EnumWatcherFactsFailureReason
    detail: str


class ModelWatcherFactsResult(BaseModel):
    """The pass's outcome: the written event and facts, or the typed failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumWatcherFactsStatus
    correlation_id: UUID
    written: ModelWatcherFactsWritten | None = None
    facts: ModelLandingFacts | None = None
    failure_reason: EnumWatcherFactsFailureReason | None = None
    failure_detail: str | None = None


__all__: list[str] = [
    "EnumWatcherFactsFailureReason",
    "EnumWatcherFactsStatus",
    "ModelWatcherFactsFailed",
    "ModelWatcherFactsRequest",
    "ModelWatcherFactsResult",
    "ModelWatcherFactsWritten",
]
