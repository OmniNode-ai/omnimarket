# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One byte-offset capture request and its checkpoint and mapping counts."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelBeaconTraceCaptureCommand(BaseModel):
    """Read complete lines from a host-local runtime trace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    log_path: str = Field(min_length=1)
    offset: int = Field(ge=0)
    lane: str | None = None
    host: str = Field(min_length=1)
    dry_run: bool = False


class ModelBeaconTraceCaptureResult(BaseModel):
    """Mapped IDs and counts include dry runs; published counts actual sends."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    next_offset: int = Field(ge=0)
    lines_read: int = Field(ge=0)
    events_published: int = Field(ge=0)
    skipped: int = Field(ge=0)
    malformed: int = Field(ge=0)
    event_ids: tuple[UUID, ...]
    hook_event_counts: dict[str, int]
