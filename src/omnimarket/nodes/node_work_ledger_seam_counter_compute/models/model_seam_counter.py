# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the work-ledger seam counter (OMN-21849)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumSeamWriterPath(StrEnum):
    """Which path wrote a ledger row."""

    BUS = "bus"
    FILE_MODE = "file_mode"
    RELAY = "relay"


class EnumSeamVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class EnumSeamLocation(StrEnum):
    """Where an unexplained file row stopped."""

    ABSENT_FROM_TOPIC = "absent_from_topic"
    ON_TOPIC_NOT_PROJECTED = "on_topic_not_projected"


class ModelSeamWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    since: datetime
    until: datetime

    @model_validator(mode="after")
    def _ordered_and_aware(self) -> ModelSeamWindow:
        if self.since.tzinfo is None or self.until.tzinfo is None:
            raise ValueError("a seam window is bounded by timezone-aware instants")
        if self.until < self.since:
            raise ValueError("a seam window ends at or after it starts")
        return self


class ModelSeamRowRef(BaseModel):
    """One ledger-file row as the seam counter lists it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    lane: str
    writer_path: EnumSeamWriterPath


class ModelSeamRefusal(BaseModel):
    """A file row the bus path refused, with the reason it was refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ModelSeamCounterRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    window: ModelSeamWindow
    ledger_id: str = Field(default="rolling-work-ledger", min_length=1)


class ModelSeamUnexplainedRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelSeamRowRef
    location: EnumSeamLocation


class ModelSeamCountedEvent(BaseModel):
    """The typed per-window count, emitted on the contract's topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["work_ledger_seam_counted"] = "work_ledger_seam_counted"
    correlation_id: UUID
    ledger_id: str
    window: ModelSeamWindow
    file_count: int = Field(ge=0)
    topic_count: int = Field(ge=0)
    projection_count: int = Field(ge=0)
    projected_count: int = Field(ge=0)
    refused_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    unexplained_count: int = Field(ge=0)
    conserved: bool
    unexplained_row_ids: list[str]


class ModelSeamCounterResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumSeamVerdict
    event: ModelSeamCountedEvent
    unexplained: list[ModelSeamUnexplainedRow]


class ModelSeamCounterOverlay(BaseModel):
    """Deployment facts of the seam counter, supplied by the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory", "redpanda"]
    bootstrap_servers: str | None = None
    consumer_group: str | None = None
    topics: list[str] = Field(default_factory=list)
    ledger_file: str | None = None
    writer_path_markers: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "EnumSeamLocation",
    "EnumSeamVerdict",
    "EnumSeamWriterPath",
    "ModelSeamCountedEvent",
    "ModelSeamCounterOverlay",
    "ModelSeamCounterRequest",
    "ModelSeamCounterResult",
    "ModelSeamRefusal",
    "ModelSeamRowRef",
    "ModelSeamUnexplainedRow",
    "ModelSeamWindow",
]
