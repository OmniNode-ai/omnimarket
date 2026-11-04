# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the file-mode replay queue (OMN-21849)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnumReplayOperation(StrEnum):
    ENQUEUE = "enqueue"
    DRAIN = "drain"


class ModelFileModeReplayRequest(BaseModel):
    """Queue rows appended in file mode, or drain the queue to the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumReplayOperation
    rows: list[str] = Field(
        default_factory=list,
        description="The exact ledger rows as appended; read for enqueue only.",
    )
    ledger_id: str = Field(default="rolling-work-ledger", min_length=1)


class ModelReplayPublishOutcome(BaseModel):
    """What the bus path said about one row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    published: bool
    refusal: str | None = Field(
        default=None, description="Set when the row can never be typed or published."
    )
    error: str | None = Field(
        default=None, description="Set when the bus refused this attempt."
    )


class ModelQueuedRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    row: str
    state: Literal["pending", "replayed", "refused"] = "pending"
    attempts: int = Field(default=0, ge=0)
    last_reason: str | None = None


class ModelReplayPending(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    attempts: int
    last_reason: str


class ModelReplayRefused(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    reason: str = Field(min_length=1)


class ModelFileModeReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumReplayOperation
    enqueued: int = 0
    already_queued: int = 0
    replayed: list[str] = Field(default_factory=list)
    still_pending: list[ModelReplayPending] = Field(default_factory=list)
    refused: list[ModelReplayRefused] = Field(default_factory=list)


class ModelReplayOverlay(BaseModel):
    """Deployment facts of the replay queue, supplied by the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory", "file"]
    queue_path: str | None = None


__all__ = [
    "EnumReplayOperation",
    "ModelFileModeReplayRequest",
    "ModelFileModeReplayResult",
    "ModelQueuedRow",
    "ModelReplayOverlay",
    "ModelReplayPending",
    "ModelReplayPublishOutcome",
    "ModelReplayRefused",
]
