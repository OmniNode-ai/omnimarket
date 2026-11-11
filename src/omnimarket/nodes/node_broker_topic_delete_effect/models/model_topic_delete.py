# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed command, live evidence and result of the broker topic delete (OMN-23879).

The command names the topics and the class the plan gave each; it never plans. The result carries
every named topic by status — deleted, would delete, refused, absent, failed — the reasons of each
refusal, and the deletes and partitions the broker lost. Nothing is deleted unless the overlay
enforces it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_broker_topic_removal_effect.models.model_broker_topic_removal import (
    EnumTopicClass,
)


class ModelDeleteTarget(BaseModel):
    """One topic the plan named, with the class the plan gave it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    topic_class: EnumTopicClass


class ModelTopicDeleteCommand(BaseModel):
    """Delete exactly the named topics; an enforcing overlay reads the approval reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    topics: tuple[ModelDeleteTarget, ...] = Field(min_length=1)
    #: The operator's go (a ledger row id); required when the overlay enforces.
    approval_ref: str | None = None
    #: Whether a ruling lets a contract-declared never-produced topic be deleted.
    ruling_covers_declared: bool = False
    #: The ledger row of that ruling; read only with ``ruling_covers_declared``.
    ruling_ref: str | None = None

    @field_validator("topics")
    @classmethod
    def _names_unique(
        cls, value: tuple[ModelDeleteTarget, ...]
    ) -> tuple[ModelDeleteTarget, ...]:
        names = [target.name for target in value]
        if len(set(names)) != len(names):
            raise ValueError("duplicate topic names")
        return value


class EnumTopicDeleteStatus(StrEnum):
    DELETED = "deleted"
    WOULD_DELETE = "would_delete"
    REFUSED = "refused"
    ABSENT = "absent"
    FAILED = "failed"


class ModelLiveTopicEvidence(BaseModel):
    """What the broker says about one topic right now, read fresh for the delete."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exists: bool
    partitions: int = Field(ge=0)
    #: Sum of the partitions' high watermarks.
    high_watermark_total: int = Field(ge=0)
    #: Sum over the partitions of high minus low watermark.
    retained_records: int = Field(ge=0)
    #: Consumer groups with a live member assigned to the topic.
    consumer_groups: tuple[str, ...]
    #: Groups with a committed offset on the topic.
    offset_groups: tuple[str, ...]
    #: Timestamp in ms of the newest retained record; ``None`` when none is retained.
    last_record_ms: int | None


class ModelTopicDeleteEntry(BaseModel):
    """One named topic's outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    status: EnumTopicDeleteStatus
    reasons: tuple[str, ...] = ()
    partitions: int = Field(ge=0)


class ModelTopicDeleteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    #: ``enforced`` deletes were issued, ``report_only`` names what would go, ``refused`` neither.
    status: Literal["enforced", "report_only", "refused"]
    refusal_reasons: tuple[str, ...] = ()
    entries: tuple[ModelTopicDeleteEntry, ...] = ()
    deletes_issued: int = 0
    #: Partitions of the entries that were deleted.
    partitions_removed: int = 0

    @property
    def failed(self) -> bool:
        """A refusal is a failed run; so is any topic whose delete failed."""
        return self.status == "refused" or any(
            entry.status == EnumTopicDeleteStatus.FAILED for entry in self.entries
        )


__all__ = [
    "EnumTopicClass",
    "EnumTopicDeleteStatus",
    "ModelDeleteTarget",
    "ModelLiveTopicEvidence",
    "ModelTopicDeleteCommand",
    "ModelTopicDeleteEntry",
    "ModelTopicDeleteResult",
]
