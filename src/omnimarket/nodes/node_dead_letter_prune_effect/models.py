# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed surface of node_dead_letter_prune_effect (OMN-17001).

A row is archived byte-exact: bytea columns travel as base64, so the archive
holds exactly what event_ledger held whatever the payload encoding. The line
format matches the one-shot dead-letter archive of 2026-09-25 (lane
dlq-cleanup-83), so one reader opens both.
"""

from __future__ import annotations

import base64
import datetime as dt
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.topic_archive.models import EnumArchiveEncryption


def _b64(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _unb64(data: str | None) -> bytes | None:
    return (
        None if data is None else base64.b64decode(data.encode("ascii"), validate=True)
    )


class ModelDeadLetterRow(BaseModel):
    """One event_ledger row, every column, bytea as base64."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_entry_id: str
    topic: str
    partition: int = Field(ge=0)
    kafka_offset: int = Field(ge=0)
    event_key_b64: str | None
    event_value_b64: str
    onex_headers: dict[str, object]
    envelope_id: str | None
    correlation_id: str | None
    event_type: str | None
    source: str | None
    event_timestamp: dt.datetime | None
    ledger_written_at: dt.datetime

    @classmethod
    def from_values(
        cls,
        *,
        ledger_entry_id: str,
        topic: str,
        partition: int,
        kafka_offset: int,
        event_key: bytes | None,
        event_value: bytes,
        onex_headers: dict[str, object],
        envelope_id: str | None,
        correlation_id: str | None,
        event_type: str | None,
        source: str | None,
        event_timestamp: dt.datetime | None,
        ledger_written_at: dt.datetime,
    ) -> ModelDeadLetterRow:
        value = _b64(event_value)
        assert value is not None
        return cls(
            ledger_entry_id=ledger_entry_id,
            topic=topic,
            partition=partition,
            kafka_offset=kafka_offset,
            event_key_b64=_b64(event_key),
            event_value_b64=value,
            onex_headers=onex_headers,
            envelope_id=envelope_id,
            correlation_id=correlation_id,
            event_type=event_type,
            source=source,
            event_timestamp=event_timestamp,
            ledger_written_at=ledger_written_at,
        )

    def value_bytes(self) -> bytes:
        out = _unb64(self.event_value_b64)
        assert out is not None
        return out

    def key_bytes(self) -> bytes | None:
        return _unb64(self.event_key_b64)

    def day_basis(self) -> dt.datetime:
        """The instant that decides a row's UTC day, as event_ledger indexes it:
        coalesce(event_timestamp, ledger_written_at)."""
        return (self.event_timestamp or self.ledger_written_at).astimezone(dt.UTC)


class ModelDeadLetterDay(BaseModel):
    """One (topic, partition, UTC day) of dead-letter rows eligible for pruning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    partition: int
    day: dt.date
    row_count: int = Field(ge=0)


class ModelDeadLetterWindowManifest(BaseModel):
    """Everything needed to find, verify and restore one archive object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Not topic-shaped: this names a file format, not a bus topic.
    schema_version: Literal["dead-letter-archive/1"] = "dead-letter-archive/1"
    format: Literal["jsonl"] = "jsonl"
    compression: Literal["gzip"] = "gzip"
    source_table: str
    day_basis: Literal["coalesce(event_timestamp, ledger_written_at) UTC"] = (
        "coalesce(event_timestamp, ledger_written_at) UTC"
    )
    topic: str
    partition: int
    day: dt.date
    first_offset: int
    last_offset: int
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


class EnumDeadLetterDayStatus(StrEnum):
    PRUNED = "pruned"  # every window archived, verified, and deleted
    DRY_RUN = "dry_run"  # eligible, nothing read, written or deleted
    FAILED = "failed"  # a window failed verification or the delete; see detail


class EnumDeadLetterPruneVerdict(StrEnum):
    PRUNED = "pruned"
    DRY_RUN = "dry_run"
    FAILED = "failed"
    REFUSED = "refused"  # a precondition failed; nothing was read or written
    NOTHING_TO_PRUNE = "nothing_to_prune"
    # OMN-19657: a scheduled (runtime-tick-driven) invocation arrived before
    # config.dead_letter_prune.schedule.run_interval_seconds had elapsed since
    # the last scheduled run. Nothing was read, written or deleted -- distinct
    # from NOTHING_TO_PRUNE, where the store WAS queried and had no eligible day.
    SKIPPED_INTERVAL_NOT_ELAPSED = "skipped_interval_not_elapsed"


class ModelDeadLetterDayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    partition: int
    day: dt.date
    rows_eligible: int
    rows_archived: int = 0
    rows_pruned: int = 0
    manifests: list[str] = Field(default_factory=list)
    reused_manifests: list[str] = Field(
        default_factory=list,
        description="Windows already archived and verified by an earlier run.",
    )
    status: EnumDeadLetterDayStatus
    detail: str = ""


class ModelDeadLetterPruneRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    retention_days: int | None = Field(
        default=None,
        ge=1,
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


class ModelDeadLetterPruneResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumDeadLetterPruneVerdict
    cutoff_day: dt.date = Field(description="Rows on this UTC day and later are kept.")
    sink_location: str
    days: list[ModelDeadLetterDayResult] = Field(default_factory=list)
    detail: str = ""

    @property
    def rows_eligible(self) -> int:
        return sum(d.rows_eligible for d in self.days)

    @property
    def rows_pruned(self) -> int:
        return sum(d.rows_pruned for d in self.days)


class ModelDeadLetterPruneScheduleConfig(BaseModel):
    """The node's contract config.dead_letter_prune.schedule block, typed.

    OMN-19657: the daily cadence declared in contract/config, the same shape
    node_github_pr_poller_effect declares poll_interval_seconds. Never a cron
    expression or a launchd plist -- the runtime-tick subscription in
    input_subscriptions is the trigger, and this interval gates how often the
    handler actually acts on a tick.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_interval_seconds: int = Field(
        ge=1,
        description=(
            "Minimum seconds between successive scheduled runs. Ticks arrive "
            "far more often than this; the handler skips every tick until "
            "this many seconds have elapsed since the last scheduled run."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "Scheduled runs pass this as ModelDeadLetterPruneRequest.dry_run. "
            "True proves the schedule fires and touches nothing; flipped to "
            "False once the dry-run pass is proven on the lab lane."
        ),
    )


class ModelDeadLetterPruneConfig(BaseModel):
    """The node's contract config block, typed."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    table: Literal["event_ledger"]
    topic_like: str
    retention_days: int = Field(ge=1)
    dsn_env: str
    max_rows_per_object: int = Field(ge=1)
    delete_batch_size: int = Field(ge=1)
    local_dir_env: str
    schedule: ModelDeadLetterPruneScheduleConfig = Field(
        default_factory=lambda: ModelDeadLetterPruneScheduleConfig(
            run_interval_seconds=86400
        ),
        description="OMN-19657: the daily runtime-tick schedule; see contract.yaml.",
    )
