# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed surface of node_projection_dlq_redrive_effect."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRedriveOutcome(StrEnum):
    REPROJECTED = "reprojected"
    DRY_RUN = "dry_run"
    SKIPPED_NO_SOURCE = "skipped_no_source_coordinates"
    SKIPPED_SOURCE_GONE = "skipped_source_unavailable"
    SKIPPED_TENANT_UNKNOWN = "skipped_tenant_unknown"
    NOT_PROJECTED = "not_projected"
    FAILED = "failed"


class EnumRedriveVerdict(StrEnum):
    REDRIVEN = "redriven"
    PARTIAL = "partial"
    NOTHING_TO_REDRIVE = "nothing_to_redrive"
    DRY_RUN = "dry_run"


class ModelProjectionDlqRedriveRequest(BaseModel):
    """Which dead letters to redrive.

    ``correlation_ids`` names the refused records; an empty list means every
    dead letter on the topic. ``dlq_topic`` defaults to the contract's.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_ids: list[str] = Field(default_factory=list)
    dlq_topic: str | None = None
    dry_run: bool = False


class ModelDeadLetterSource(BaseModel):
    """The fields of a dead-letter envelope the redrive reads."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = ""
    failure_reason: str = ""
    source_topic: str | None = None
    source_partition: int | None = None
    source_offset: int | None = None

    def has_source(self) -> bool:
        return (
            bool(self.source_topic)
            and self.source_partition is not None
            and self.source_offset is not None
        )


class ModelRedriveRecordResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumRedriveOutcome
    detail: str = ""


class ModelProjectionDlqRedriveResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dlq_topic: str
    dead_letters_read: int
    reprojected: int
    skipped: int
    failed: int
    verdict: EnumRedriveVerdict
    records: list[ModelRedriveRecordResult]
