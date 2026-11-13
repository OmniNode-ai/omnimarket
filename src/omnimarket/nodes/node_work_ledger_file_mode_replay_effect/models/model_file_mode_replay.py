# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the file-mode replay queue (OMN-21849)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumReplayOperation(StrEnum):
    ENQUEUE = "enqueue"
    DRAIN = "drain"
    REPLAY_FROM_FILE = "replay_from_file"


class ModelFileModeReplayRequest(BaseModel):
    """Queue rows appended in file mode, or drain the queue to the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumReplayOperation
    rows: list[str] = Field(
        default_factory=list,
        description="The exact ledger rows as appended; read for enqueue only.",
    )
    ledger_id: str = Field(default="rolling-work-ledger", min_length=1)
    row_ids: list[str] = Field(
        default_factory=list,
        description="replay_from_file: the row ids (sha256 of the trimmed row) to re-emit from the ledger file.",
    )
    window_since: datetime | None = Field(
        default=None,
        description="replay_from_file: the first stamp (UTC, inclusive) of the rows to re-emit.",
    )
    window_until: datetime | None = Field(
        default=None,
        description="replay_from_file: the last stamp (UTC, inclusive) of the rows to re-emit.",
    )

    @model_validator(mode="after")
    def _replay_selection_is_exact(self) -> ModelFileModeReplayRequest:
        replay = self.operation is EnumReplayOperation.REPLAY_FROM_FILE
        windowed = self.window_since is not None or self.window_until is not None
        if not replay and (self.row_ids or windowed):
            raise ValueError("row_ids and a window are read by replay_from_file only")
        if not replay:
            return self
        if bool(self.row_ids) == windowed:
            raise ValueError(
                "replay_from_file takes row ids or a time window, exactly one of them"
            )
        if windowed and (self.window_since is None or self.window_until is None):
            raise ValueError("a replay window needs both window_since and window_until")
        return self


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
    selected: int = Field(
        default=0, description="replay_from_file: rows the file held for the selection."
    )
    already_replayed: list[str] = Field(
        default_factory=list,
        description="replay_from_file: selected rows the queue already shows replayed; not sent again.",
    )
    not_in_file: list[str] = Field(
        default_factory=list,
        description="replay_from_file: requested row ids the ledger file does not hold; nothing was sent for them.",
    )


class ModelReplayOverlay(BaseModel):
    """Deployment facts of the replay queue, supplied by the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory", "file"]
    queue_path: str | None = None
    ledger_path: str | None = Field(
        default=None,
        description="replay_from_file: the ledger file of record the rows are read from.",
    )
    archive_dir: str | None = Field(
        default=None,
        description="replay_from_file: the directory of the ledger's archive splits, read with the file.",
    )
    archive_glob: str | None = Field(
        default=None,
        description="replay_from_file: the file-name pattern of the archive splits inside archive_dir.",
    )
    replay_source: str | None = Field(
        default=None,
        description="replay_from_file: the source cell the re-emitted rows carry (the projection's backfill source).",
    )


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
