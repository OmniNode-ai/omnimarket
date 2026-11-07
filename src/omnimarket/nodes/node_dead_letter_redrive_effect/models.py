# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed surface of node_dead_letter_redrive_effect (OMN-22811).

A dead-letter envelope names the record it came from (source topic, partition,
offset, OMN-22811). The redrive reads that record back from its source with a
group-less, commit-free read and publishes it again to the same topic. No
consumer group's offset is touched.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRedriveVerdict(StrEnum):
    REDRIVEN = "REDRIVEN"
    DRY_RUN = "DRY_RUN"
    NOTHING_TO_REDRIVE = "NOTHING_TO_REDRIVE"
    PARTIAL = "PARTIAL"


class EnumRedriveItemStatus(StrEnum):
    REDRIVEN = "REDRIVEN"
    WOULD_REDRIVE = "WOULD_REDRIVE"
    SKIPPED_NO_SOURCE = "SKIPPED_NO_SOURCE"
    SKIPPED_SOURCE_IS_DLQ = "SKIPPED_SOURCE_IS_DLQ"
    SKIPPED_SOURCE_GONE = "SKIPPED_SOURCE_GONE"
    SKIPPED_ALREADY_REDRIVEN = "SKIPPED_ALREADY_REDRIVEN"
    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class ModelDeadLetterRedriveRequest(BaseModel):
    """What to redrive. Unset fields take the contract's configured values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_topic: str | None = Field(
        default=None, description="Redrive only records that came from this topic."
    )
    max_records: int | None = Field(default=None, ge=1)
    dry_run: bool | None = Field(
        default=None, description="None takes the contract's dry_run setting."
    )


class ModelDlqBacklogEntry(BaseModel):
    """One dead-letter record, as the backlog read returns it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dlq_topic: str
    dlq_partition: int = Field(ge=0)
    dlq_offset: int = Field(ge=0)
    correlation_id: str | None = None
    failure_reason: str | None = None
    handler: str | None = None
    source_topic: str | None = None
    source_partition: int | None = Field(default=None, ge=0)
    source_offset: int | None = Field(default=None, ge=0)

    @property
    def key(self) -> str:
        return f"{self.dlq_topic}:{self.dlq_partition}:{self.dlq_offset}"

    @property
    def has_source(self) -> bool:
        return (
            self.source_topic is not None
            and self.source_partition is not None
            and self.source_offset is not None
        )


class ModelSourceRecord(BaseModel):
    """The exact bytes of a source record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: bytes | None
    value: bytes


class ModelRedriveItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_key: str
    correlation_id: str | None
    source_topic: str | None
    source_partition: int | None
    source_offset: int | None
    status: EnumRedriveItemStatus
    detail: str = ""


class ModelDeadLetterRedriveResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumRedriveVerdict
    dry_run: bool
    backlog_read: int
    with_source: int
    without_source: int
    redriven: int
    would_redrive: int
    skipped: int
    failed: int
    items: list[ModelRedriveItem]
    detail: str = ""


class ModelDeadLetterRedriveConfig(BaseModel):
    """The ``config.dead_letter_redrive`` block of the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dlq_topic_like: str
    dlq_topic_prefix: str
    max_records: int = Field(ge=1)
    dry_run: bool
    binding: dict[str, str | None]
