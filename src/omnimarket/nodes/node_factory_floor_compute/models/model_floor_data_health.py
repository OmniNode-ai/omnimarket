# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The data-health fact of the factory floor record: one health state per source and one count per state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

SourceHealthState = Literal[
    "available", "partial", "overdue", "unavailable", "snapshot"
]


class ModelFloorSourceHealth(BaseModel):
    """One source the record was computed from, and how fresh and complete its read was."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    state: SourceHealthState
    # When the source was read (ISO UTC); None when it was never read (unavailable), which is not "just now".
    read_at: str | None
    # The source's own newest observation (ISO UTC, or a bare date for a dated snapshot); None when it carries none.
    observed_at: str | None
    # Minutes between the record's generated_at and read_at; None for an unread source and for a snapshot.
    age_min: int | None
    note: str


class ModelFloorDataHealthSummary(BaseModel):
    """One count per state, and the count of sources never read (a subset of unavailable, never drawn as 0)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    available: int
    partial: int
    overdue: int
    unavailable: int
    snapshot: int
    unread: int


class ModelFloorDataHealth(BaseModel):
    """Data health of the record: when it was generated, when it was published, and each source's read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: str
    # None until the publish node stamps it as it writes the record to the served directory.
    published_at: str | None
    # The thresholds from the contract, carried so the page applies the same two numbers the projection did.
    overdue_after_min: int
    stale_after_min: int
    sources: tuple[ModelFloorSourceHealth, ...]
    summary: ModelFloorDataHealthSummary
