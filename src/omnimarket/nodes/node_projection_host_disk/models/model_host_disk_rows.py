# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's row models and result: the newest host disk reading, per key.

One ``ModelHostDiskClassRow`` per class the event read and one
``ModelHostDiskVolumeRow`` per declared volume, keyed the two projection tables
are keyed. Every value is a field of the event, so the same event always folds
to the same rows and a replay is byte-identical.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_host_disk_reading import (
    DiskClassName,
    NonEmptyStr,
)


class ModelHostDiskClassRow(BaseModel):
    """What one host disk read event asserts about one class on one host.

    ``read_at`` is the event's reading time and the only ordering authority:
    the writer's upsert refuses a write whose ``read_at`` is not newer than the
    stored one, in the conflict arm's WHERE. The row never holds a guessed
    size: an unread class keeps ``status='unread'`` and the event's reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: NonEmptyStr
    class_name: DiskClassName
    status: str = Field(..., pattern="^(read|unread)$")
    unread_reason: str = ""
    total_bytes: int = Field(ge=0, default=0)
    entry_count: int = Field(ge=0, default=0)
    revision_count: int = Field(ge=0, default=0)
    #: One JSON-ready dict per group the event reported, largest first, exactly
    #: as ``ModelDiskGroup.model_dump(mode="json")`` produced it.
    groups: tuple[dict[str, Any], ...] = ()
    groups_omitted: int = Field(ge=0, default=0)
    unreadable_process_count: int = Field(ge=0, default=0)
    read_at: datetime
    correlation_id: NonEmptyStr

    @model_validator(mode="after")
    def _read_at_carries_a_timezone(self) -> Self:
        if self.read_at.utcoffset() is None:
            msg = "read_at must carry a timezone"
            raise ValueError(msg)
        return self

    def groups_json(self) -> str:
        """The groups as canonical JSON: the same reading, the same bytes."""
        return json.dumps(list(self.groups), sort_keys=True, separators=(",", ":"))


class ModelHostDiskVolumeRow(BaseModel):
    """What one host disk read event asserts about one declared volume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: NonEmptyStr
    volume_path: NonEmptyStr
    total_bytes: int = Field(..., ge=0)
    free_bytes: int = Field(..., ge=0)
    used_bytes: int = Field(..., ge=0)
    read_at: datetime
    correlation_id: NonEmptyStr

    @model_validator(mode="after")
    def _read_at_carries_a_timezone(self) -> Self:
        if self.read_at.utcoffset() is None:
            msg = "read_at must carry a timezone"
            raise ValueError(msg)
        return self


class ModelHostDiskProjectionResult(BaseModel):
    """The fold's output: one row per class and one row per volume in the event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: NonEmptyStr
    correlation_id: NonEmptyStr
    read_at: datetime
    class_rows: tuple[ModelHostDiskClassRow, ...] = Field(min_length=1)
    volume_rows: tuple[ModelHostDiskVolumeRow, ...] = Field(min_length=1)


class ModelHostDiskProjectionApplied(BaseModel):
    """The projection-applied event: what one host disk reading changed in the projection.

    ``rows_refused`` counts rows the ordering guard declined (an older reading or a redelivery); it is not an error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: NonEmptyStr
    host_name: NonEmptyStr
    rows_upserted: int = Field(ge=0)
    rows_refused: int = Field(ge=0)


__all__: list[str] = [
    "ModelHostDiskClassRow",
    "ModelHostDiskProjectionApplied",
    "ModelHostDiskProjectionResult",
    "ModelHostDiskVolumeRow",
]
