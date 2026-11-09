# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The record the MVP launch block's scheduled read keeps (OMN-23205, plan task M1).

One read per producer workflow: the runs the source-control node listed for it, or the reason it could not be listed. The floor's
command route answers the block's questions from this record, so a publish never waits on the source-control reads.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.floor_17a.model_mvp_launch import ModelMvpRun


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelMvpSourceRead(_Frozen):
    """The newest runs of one producer workflow as read at ``read_at``; ``error`` set means the workflow could not be read."""

    repo: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    read_at: datetime
    runs: tuple[ModelMvpRun, ...] = ()
    error: str | None = None


class ModelMvpReads(_Frozen):
    """The record file: every producer workflow's last read, newest first within a read."""

    updated_at: datetime
    reads: tuple[ModelMvpSourceRead, ...] = ()
