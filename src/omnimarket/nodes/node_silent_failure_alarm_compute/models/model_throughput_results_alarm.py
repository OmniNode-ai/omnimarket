# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the factory produced in the last hour in, the throughput-results verdict out (OMN-19998)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelThroughputResultsRequest(BaseModel):
    """Counts over the hour before ``now``; None is a source the caller could not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    merges_last_hour: int | None = Field(default=None, ge=0)
    # Open, non-draft PRs: merges are owed only while one waits.
    open_prs: int | None = Field(default=None, ge=0)
    lanes_placed_last_hour: int | None = Field(default=None, ge=0)
    # The throughput tick's own merge bound (--min-merges-per-hour). Zero turns a measure off.
    min_merges_per_hour: int = Field(default=5, ge=0)
    min_lanes_placed_per_hour: int = Field(default=1, ge=0)


class ModelThroughputResultsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    alarm: bool
    state: Literal["PRODUCING", "SHORT", "UNKNOWN"]
    short: tuple[str, ...] = ()
    fix: str = ""
    friction_cells: str = ""
    alert_text: str = ""
