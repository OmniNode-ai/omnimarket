# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The data-health thresholds and snapshot declarations of the factory floor, as the compute contract names them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelFloorDataHealthConfig(BaseModel):
    """Thresholds named once, in the compute node's contract; the projection and the page read them from there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A source read longer ago than this is overdue.
    overdue_after_min: int = Field(gt=0)
    # A record older than this is stale: the page says "Stale, not live". Must exceed overdue_after_min.
    stale_after_min: int = Field(gt=0)
    # Sources declared as dated snapshots that do not refresh.
    snapshot_sources: tuple[str, ...] = ()


class ModelFloorSnapshotBasis(BaseModel):
    """The date (or time) a declared snapshot source is as-of; a date-only basis stays a date."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1)
    basis: str = Field(min_length=1)
