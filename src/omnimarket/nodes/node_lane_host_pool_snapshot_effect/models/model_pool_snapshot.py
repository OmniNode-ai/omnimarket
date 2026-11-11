# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane host pool snapshot writer's request, per host rows, record, result and failed event (OMN-23664).

The record is what the snapshot file cannot carry: the read time and, per host, the declared slots,
running lanes, free slots, account labels usable and limited, and the away state. The snapshot file
itself is a ModelLaneHostPoolSnapshot and holds no other key.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnumPoolSnapshotStatus(StrEnum):
    WRITTEN = "written"
    OBSERVED = "observed"
    FAILED = "failed"


class EnumPoolSnapshotFailureReason(StrEnum):
    ADVERTISEMENTS_UNREAD = "advertisements_unread"
    HOST_TABLE_UNREAD = "host_table_unread"
    LANES_UNREAD = "lanes_unread"
    ACCOUNTS_UNREAD = "accounts_unread"
    NO_HOSTS = "no_hosts"
    WRITE_FAILED = "write_failed"
    PUBLISH_FAILED = "publish_failed"


class ModelHostAccounts(BaseModel):
    """The account availability for one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(..., min_length=1)
    usable: tuple[str, ...] = ()
    limited: tuple[str, ...] = ()
    unread_reason: str | None = None


class ModelTokenPoolEntryReading(BaseModel):
    """One token's label and state, read from the token pool command's status JSON; every other key is ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    label: str = Field(..., min_length=1)
    limited: bool = False
    revoked: bool = False


class ModelTokenPoolStatusReading(BaseModel):
    """The slice of the token pool command's status JSON this node reads (labels and whether each is limited).

    Declared here, not imported from the token pool node's models, so this node does not reach into a sibling's models.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    tokens: tuple[ModelTokenPoolEntryReading, ...] = ()


class ModelPoolSnapshotRequest(BaseModel):
    """Take the snapshot of the lane host pool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    write: bool = True


class ModelPoolHostRow(BaseModel):
    """One host's lane and account state in the snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    declared_slots: int = Field(..., ge=0)
    running_lanes: int = Field(..., ge=0)
    free_slots: int = Field(..., ge=0)
    accounts_usable: tuple[str, ...]
    accounts_limited: tuple[str, ...]
    away: bool
    away_reason: str | None = None
    refusal: str | None = None
    advertised_at: datetime | None = None


class ModelPoolSnapshotRecord(BaseModel):
    """The snapshot of the lane host pool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    read_at: datetime
    max_age_seconds: int = Field(..., ge=1)
    hosts: tuple[ModelPoolHostRow, ...]
    free_slots_total: int = Field(..., ge=0)
    snapshot_path: str
    provenance: str

    @field_validator("read_at")
    @classmethod
    def _require_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "read_at must be timezone-aware"
            raise ValueError(msg)
        return value


class ModelPoolSnapshotFailedEvent(BaseModel):
    """Emitted when the lane host pool snapshot could not be taken."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    reason: EnumPoolSnapshotFailureReason
    detail: str
    read_at: datetime


class ModelPoolSnapshotResult(BaseModel):
    """The snapshot's record, or its typed failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    status: EnumPoolSnapshotStatus
    record: ModelPoolSnapshotRecord | None = None
    failure_reason: EnumPoolSnapshotFailureReason | None = None
    failure_detail: str = ""


__all__: list[str] = [
    "EnumPoolSnapshotFailureReason",
    "EnumPoolSnapshotStatus",
    "ModelHostAccounts",
    "ModelPoolHostRow",
    "ModelPoolSnapshotFailedEvent",
    "ModelPoolSnapshotRecord",
    "ModelPoolSnapshotRequest",
    "ModelPoolSnapshotResult",
    "ModelTokenPoolEntryReading",
    "ModelTokenPoolStatusReading",
]
