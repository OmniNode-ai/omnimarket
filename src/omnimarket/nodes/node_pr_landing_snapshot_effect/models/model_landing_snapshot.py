# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step's request, per-PR write facts, tick record, result and events (OMN-23409).

The step reads one tick's facts from its declared sources, fills each pull request's GraphQL node
id and the head's failed run ids from the write-facts source, and records the filled facts of
that tick on the bus. Every tick gets its own record, so a tick's facts are recoverable after the
next tick (the script overwrites its one facts file every tick and keeps none).
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import (
    PR_KEY_PATTERN,
    SHA_PATTERN,
)
from omnimarket.models.landing_tick_record import ModelLandingTickRecord


class EnumSnapshotStatus(StrEnum):
    RECORDED = "recorded"
    FAILED = "failed"


class EnumSnapshotFailureReason(StrEnum):
    FACTS_UNREAD = "facts_unread"
    WRITE_FACTS_UNREAD = "write_facts_unread"
    TICK_MISMATCH = "tick_mismatch"
    TICK_ALREADY_RECORDED = "tick_already_recorded"


class ModelPrWriteFacts(BaseModel):
    """What the act-mode writer needs of one PR and the facts source does not carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    head_sha: str = Field(
        ...,
        pattern=SHA_PATTERN,
        description="The head these were read at; a moved head fills nothing.",
    )
    pr_node_id: str | None = Field(default=None, min_length=1)
    failed_run_ids: tuple[int, ...] = ()


class ModelLandingSnapshotRequest(BaseModel):
    """Take the snapshot of one tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tick: int = Field(..., ge=1)


class ModelLandingSnapshotResult(BaseModel):
    """The tick's record, or its typed failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tick: int
    status: EnumSnapshotStatus
    record: ModelLandingTickRecord | None = None
    node_ids_filled: int = 0
    run_ids_filled: int = 0
    failure_reason: EnumSnapshotFailureReason | None = None
    failure_detail: str = ""


class ModelLandingSnapshotFailedEvent(BaseModel):
    """Emitted when a tick's facts could not be recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tick: int
    reason: EnumSnapshotFailureReason
    detail: str


__all__: list[str] = [
    "EnumSnapshotFailureReason",
    "EnumSnapshotStatus",
    "ModelLandingSnapshotFailedEvent",
    "ModelLandingSnapshotRequest",
    "ModelLandingSnapshotResult",
    "ModelLandingTickRecord",
    "ModelPrWriteFacts",
]
