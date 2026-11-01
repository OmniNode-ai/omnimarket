# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run request and result of the merged-and-running count reader."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_merged_running_count_compute.models.model_merged_running_count import (
    ModelMergedRunningCountResult,
    ModelMrcConfig,
)


class ModelMergedRunningCountRunRequest(BaseModel):
    """One run: an override clock (tests, replays) and an overlay config that replaces the contract's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime | None = None
    config: ModelMrcConfig | None = Field(
        default=None,
        description="a deployment overlay's config; omitted: the compute contract's declared config",
    )

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("now must carry a timezone")
        return value


class ModelMergedRunningCountRunResult(BaseModel):
    """The count, and how many non-merged pull request records the reader passed that the count left out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: ModelMergedRunningCountResult
    pr_records_read: int
    evidence_records_read: int
