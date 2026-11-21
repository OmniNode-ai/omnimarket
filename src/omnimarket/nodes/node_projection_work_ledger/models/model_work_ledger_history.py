# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the work-ledger history import, the citation table and the verb adjudication (OMN-21947)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

IMPORT_SOURCE = "archive-import"
LIVE_FILE_KEY = "live"


class ModelHistoryRow(BaseModel):
    """One ledger row to import: its identity, its projection fields and its file order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    ledger_id: str
    row_ts: datetime
    row_type: str
    row_lane: str | None
    tickets: tuple[str, ...]
    raw_row: str
    legacy_ordinal: int = Field(..., ge=1)
    file_key: str = Field(..., description="The split's file name, or 'live'.")
    first_line: int = Field(..., ge=1)
    log_only: bool = Field(
        default=False,
        description="A legacy or tool-internal row type the projection's parser refuses: "
        "kept in the log, folded into no state.",
    )


class ModelCitationEntry(BaseModel):
    """One ``ledger_line_citation`` row: a (file, generation, line) -> row_id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_id: str
    file_key: str
    generation: int = Field(..., ge=0)
    line_no: int = Field(..., ge=1)
    row_id: str


class ModelCitationResolution(BaseModel):
    """What one ``<ledger>:<line>`` citation in an ACK or amends cell resolves to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    citing_row_id: str
    field: str
    file_key: str
    generation: int
    line_no: int
    row_id: str | None = Field(
        default=None,
        description="None when no such line existed when the citing row was written.",
    )


class ModelHistoryImportPlan(BaseModel):
    """Everything the import will write, computed from the files alone (pure)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_id: str
    rows: tuple[ModelHistoryRow, ...]
    citations: tuple[ModelCitationEntry, ...]
    resolutions: tuple[ModelCitationResolution, ...] = ()
    duplicate_rows: int = Field(
        ..., ge=0, description="Repeated row text; the first keeps its ordinal."
    )
    unprojectable_rows: tuple[str, ...] = Field(
        default=(),
        description="row_ids imported log-only: the projection's parser refuses their type.",
    )
    header_shifts: dict[int, int] = Field(
        default_factory=dict,
        description="Per live generation, lines the header differs from the current live file's.",
    )
    oldest_row_ts: datetime | None
    oldest_file_key: str | None


class ModelLedgerSeqFingerprint(BaseModel):
    """The assigned ``ledger_seq`` column, as one hash the import must not move."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assigned_rows: int
    distinct_seq: int
    min_seq: int | None
    max_seq: int | None
    sha256: str
    min_row_ts: datetime | None
    total_rows: int


class ModelHistoryImportReport(BaseModel):
    """The outcome of one ``apply``: counts and the before/after fingerprint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planned_rows: int
    inserted_rows: int
    ordinals_set_on_existing: int
    state_ops_applied: int
    citations_inserted: int
    before: ModelLedgerSeqFingerprint
    after: ModelLedgerSeqFingerprint

    @property
    def ledger_seq_unchanged(self) -> bool:
        return (
            self.before.sha256 == self.after.sha256
            and self.before.assigned_rows == self.after.assigned_rows
            and self.before.distinct_seq == self.after.distinct_seq
        )


class EnumVerbDiffClass(StrEnum):
    """Why a hold or claim shows in one source's answer and not the other's."""

    FILE_WINDOW_TRUNCATION = "file_window_truncation"
    DATABASE_FOLD_DIFFERENCE = "database_fold_difference"
    BEFORE_FIRST_DB_ROW = "before_first_db_row"
    UNCLASSIFIED = "unclassified"


class EnumVerbSide(StrEnum):
    FILE = "file"
    DB = "db"


class ModelVerbItem(BaseModel):
    """One answer item of ``holds`` or ``open-claims`` from one source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verb: str
    key: str
    opened_at: datetime | None
    until_open: bool = False


class ModelDiffWindow(BaseModel):
    """The two reads' extents, which decide a difference's class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_first_stamp: datetime = Field(
        ..., description="Oldest row the file read covered."
    )
    file_reads_archives: bool
    db_first_row_ts: datetime = Field(
        ..., description="The database's earliest row (min row_ts)."
    )
    as_of: datetime


class ModelClassifiedDiff(BaseModel):
    """One differing item, the side that has it, its class and the side that is wrong."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item: ModelVerbItem
    present_in: EnumVerbSide
    diff_class: EnumVerbDiffClass
    wrong_side: EnumVerbSide
    fix: str


class ModelStaleHoldEntry(BaseModel):
    """An open-ended hold nothing released, older than 14 days: listed apart, not in force."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    opened_at: datetime
    age_days: int


class ModelVerbAdjudication(BaseModel):
    """The classified differences of one verb, and the stale holds set aside first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verb: str
    shared: int
    diffs: tuple[ModelClassifiedDiff, ...]
    stale: tuple[ModelStaleHoldEntry, ...] = ()

    def counts(self) -> dict[str, int]:
        found = {kind.value: 0 for kind in EnumVerbDiffClass}
        for diff in self.diffs:
            found[diff.diff_class.value] += 1
        return found

    @property
    def agrees(self) -> bool:
        """True when, after the stale rule, both sources hold the same items."""
        return not self.diffs


__all__: list[str] = [
    "IMPORT_SOURCE",
    "LIVE_FILE_KEY",
    "EnumVerbDiffClass",
    "EnumVerbSide",
    "ModelCitationEntry",
    "ModelCitationResolution",
    "ModelClassifiedDiff",
    "ModelDiffWindow",
    "ModelHistoryImportPlan",
    "ModelHistoryImportReport",
    "ModelHistoryRow",
    "ModelLedgerSeqFingerprint",
    "ModelStaleHoldEntry",
    "ModelVerbAdjudication",
    "ModelVerbItem",
]
