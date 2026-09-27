# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed surface of node_consumer_flow_prune_effect (OMN-19658).

A row is archived with every column of omninode_internal.consumer_flow_windows,
timestamps normalised to UTC, so the same row always encodes to the same bytes.
That property is what the pre-delete check relies on: the rows the source still
holds under the archived projection cursors must encode to exactly the archived
plaintext, or nothing in that window is deleted.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.topic_archive.models import EnumArchiveEncryption

#: The operator's floor (OPERATOR-CONSENT 2026-09-25T18:27:30Z): rows 30 days
#: old or newer are never pruned, whatever a request or a contract says.
RETENTION_FLOOR_DAYS = 30


class ModelConsumerFlowRow(BaseModel):
    """One consumer_flow_windows row, every column."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer_group: str
    topic: str
    window_start: dt.datetime
    window_end: dt.datetime
    node_id: str
    ingest_sequence: int
    messages_in: int | None
    messages_out: int | None
    messages_dlq: int | None
    handler_errors: int | None
    upstream_produced: int | None
    upstream_evidence: str
    flow_state: str
    evaluated_at: dt.datetime
    projection_cursor: int = Field(ge=1)

    @field_validator("window_start", "window_end", "evaluated_at")
    @classmethod
    def _utc(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must carry a time zone")
        return value.astimezone(dt.UTC)

    def day(self) -> dt.date:
        """The UTC day that decides retention: the day of window_start."""
        return self.window_start.date()


class ModelConsumerFlowDay(BaseModel):
    """One UTC day of rows eligible for pruning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: dt.date
    row_count: int = Field(ge=0)


class ModelConsumerFlowWindowManifest(BaseModel):
    """Everything needed to find, verify and restore one archive object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Not topic-shaped: this names a file format, not a bus topic.
    schema_version: Literal["consumer-flow-archive/1"] = "consumer-flow-archive/1"
    format: Literal["jsonl"] = "jsonl"
    compression: Literal["gzip"] = "gzip"
    source_table: str
    day_basis: Literal["window_start UTC"] = "window_start UTC"
    day: dt.date
    first_cursor: int
    last_cursor: int
    record_count: int = Field(ge=1)
    plaintext_sha256: str
    object_sha256: str
    object_bytes: int
    object_name: str
    manifest_name: str
    encryption: EnumArchiveEncryption
    recipient: str | None = Field(
        default=None,
        description="Who can decrypt, never a secret: the KMS key ARN or age recipient.",
    )
    archived_at: dt.datetime


class EnumConsumerFlowDayStatus(StrEnum):
    PRUNED = "pruned"  # every window archived, verified, and deleted
    DRY_RUN = "dry_run"  # eligible, nothing read, written or deleted
    FAILED = "failed"  # a window failed verification or the delete; see detail


class EnumConsumerFlowPruneVerdict(StrEnum):
    PRUNED = "pruned"
    DRY_RUN = "dry_run"
    FAILED = "failed"
    REFUSED = "refused"  # a precondition failed; nothing was read or written
    NOTHING_TO_PRUNE = "nothing_to_prune"
    # A runtime tick arrived before schedule.run_interval_seconds had elapsed
    # since the last scheduled run: nothing was read, written or deleted.
    SKIPPED_INTERVAL_NOT_ELAPSED = "skipped_interval_not_elapsed"


class ModelConsumerFlowDayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    day: dt.date
    rows_eligible: int
    rows_archived: int = 0
    rows_pruned: int = 0
    manifests: list[str] = Field(default_factory=list)
    reused_manifests: list[str] = Field(
        default_factory=list,
        description="Windows already archived and verified by an earlier run.",
    )
    status: EnumConsumerFlowDayStatus
    detail: str = ""


class ModelConsumerFlowPruneRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    retention_days: int | None = Field(
        default=None,
        ge=RETENTION_FLOOR_DAYS,
        description="Keep this many whole UTC days; the contract's retention_days when omitted.",
    )
    as_of: dt.datetime | None = Field(
        default=None,
        description="The instant retention is measured from; now when omitted.",
    )
    dry_run: bool = False
    max_days: int | None = Field(
        default=None,
        ge=1,
        description="Stop after this many eligible days, oldest first.",
    )


class ModelConsumerFlowPruneResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumConsumerFlowPruneVerdict
    cutoff_day: dt.date = Field(description="Rows on this UTC day and later are kept.")
    sink_location: str
    days: list[ModelConsumerFlowDayResult] = Field(default_factory=list)
    detail: str = ""

    @property
    def rows_eligible(self) -> int:
        return sum(d.rows_eligible for d in self.days)

    @property
    def rows_pruned(self) -> int:
        return sum(d.rows_pruned for d in self.days)


class ModelConsumerFlowPruneScheduleConfig(BaseModel):
    """config.consumer_flow_prune.schedule: the runtime-tick cadence.

    The same shape node_dead_letter_prune_effect declares (OMN-19657), so both
    prunes run on one daily schedule. The tick is the trigger; this interval
    gates how often a tick acts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_interval_seconds: int = Field(ge=1)
    dry_run: bool = False


class ModelConsumerFlowPruneConfig(BaseModel):
    """The node's contract config block, typed."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    table: Literal["omninode_internal.consumer_flow_windows"]
    retention_days: int = Field(ge=RETENTION_FLOOR_DAYS)
    dsn_env: str
    max_rows_per_object: int = Field(ge=1)
    delete_batch_size: int = Field(ge=1)
    local_dir_env: str
    schedule: ModelConsumerFlowPruneScheduleConfig
