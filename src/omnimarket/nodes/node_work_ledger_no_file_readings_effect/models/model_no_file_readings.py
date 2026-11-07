# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the seven no-file readings (OMN-22917, plan section 4.3).

The facts are what a probe saw; the handler turns them into WORKING, NOT_WORKING or UNKNOWN. No
field here has a default that names a path, a host, a topic or a bound.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- what the probes saw -------------------------------------------------------------------------


class ModelAppendReceipt(_Frozen):
    ledger_seq: int | None
    file_line: bool = Field(description="The append left a line in a file.")
    read_back: bool = Field(description="The query node returned the probe row.")


class ModelFileProbeFacts(_Frozen):
    """Reading 1: the path and its old names on every host and clone, and a probe append."""

    paths_present: tuple[str, ...]
    receipt: ModelAppendReceipt


class ModelCensusFacts(_Frozen):
    """Reading 2: the census total and the launchd units naming the path."""

    total_files: int = Field(ge=0)
    own_line_files: int = Field(ge=0)
    units_naming_path: tuple[str, ...]


class ModelAbsenceFacts(_Frozen):
    """Reading 3: the path absent or unwritable, then every reader restarted, then one read."""

    hours_absent: float = Field(ge=0)
    writer_failures: int = Field(ge=0)
    readers_restarted: int = Field(ge=0)
    file_fallback_lines: int = Field(ge=0)
    query_read_exit: int


class ModelRowKey(_Frozen):
    seq: int
    digest: str


class ModelRebuildFacts(_Frozen):
    """Reading 4: a table rebuilt empty from the accepted-row topic against the live table."""

    live: tuple[ModelRowKey, ...]
    rebuilt: tuple[ModelRowKey, ...]


class ModelRestoreFacts(_Frozen):
    """Reading 5: the snapshot restore drill, run on another host."""

    ran: bool
    snapshot_rows: int = Field(ge=0)
    restored_rows: int = Field(ge=0)
    checksum_ok: bool


class ModelPublishFacts(_Frozen):
    """Reading 6: the publish job and its newest 'ledger rows through' commit."""

    hours_since_rows_through_commit: float | None = Field(
        description="None when no such commit exists."
    )
    job_listed: bool


class ModelReceiptFacts(_Frozen):
    """Reading 7: the daily receipt."""

    compared: tuple[str, ...]
    exact: bool
    mismatches: int = Field(ge=0)


class ModelNoFileFacts(_Frozen):
    """All seven fact sets, for a source that holds them in memory."""

    file_probe: ModelFileProbeFacts
    census: ModelCensusFacts
    absence: ModelAbsenceFacts
    rebuild: ModelRebuildFacts
    restore: ModelRestoreFacts
    publish: ModelPublishFacts
    receipt: ModelReceiptFacts


# ---- overlay -------------------------------------------------------------------------------------


class ModelReadingSpec(_Frozen):
    number: int = Field(ge=1, le=7)
    name: str = Field(min_length=1)
    injected_fault: str = Field(
        min_length=1, description="The break that must turn it red."
    )


class ModelNoFileRoute(_Frozen):
    kind: Literal["inmemory", "command"]
    command_envs: dict[str, str] = Field(
        default_factory=dict,
        description="For kind=command: reading name to the variable holding the probe's argv.",
    )
    timeout_seconds: int = Field(default=120, gt=0)


class ModelNoFileOverlay(_Frozen):
    readings: tuple[ModelReadingSpec, ...] = Field(min_length=7, max_length=7)
    absence_min_hours: float = Field(gt=0)
    publish_quiet_hours: float = Field(gt=0)
    receipt_sources: tuple[str, ...] = Field(min_length=1)
    census_total_bound: int = Field(ge=0)
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    row_source: str = Field(min_length=1)
    max_age_seconds: int = Field(gt=0)
    route: ModelNoFileRoute
    journal_env: str = Field(min_length=1)


# ---- request and result --------------------------------------------------------------------------


class ModelNoFileRequest(_Frozen):
    correlation_id: str = Field(min_length=1)
    write: bool = False


class ModelNoFileReading(_Frozen):
    number: int
    name: str
    verdict: EnumReadingVerdict
    outcome: EnumReadingOutcome
    reasons: tuple[str, ...]
    injected_fault: str
    row: str | None = None
    ledger: str = "not-written"


class ModelNoFileResult(_Frozen):
    correlation_id: str
    status: Literal["completed", "failed"]
    readings: tuple[ModelNoFileReading, ...] = ()
    all_working: bool = False
    failure_code: str | None = None
    failure_detail: str = ""
