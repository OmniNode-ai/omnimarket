# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed output of the work-ledger parity check (OMN-19513)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumParityMismatchKind(StrEnum):
    ROW_MISSING_IN_PROJECTION = "row_missing_in_projection"
    ROW_MISSING_IN_FILE = "row_missing_in_file"
    ROW_TEXT_DIFFERS = "row_text_differs"
    ROW_TEXT_MISSING_IN_PROJECTION = "row_text_missing_in_projection"
    ROW_AUTHORITY_DIFFERS = "row_authority_differs"
    STATE_MISSING_IN_PROJECTION = "state_missing_in_projection"
    STATE_OPENED_AT_DIFFERS = "state_opened_at_differs"
    STATE_CLOSED_AT_DIFFERS = "state_closed_at_differs"
    STATE_RELATIONS_DIFFER = "state_relations_differ"


class ModelParityMismatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumParityMismatchKind
    key: str = Field(
        ..., description="row_id for row mismatches, entity_key for state."
    )
    detail: str = ""


class EnumParityLossClass(StrEnum):
    """Where a row missing from the projection was lost (OMN-20535 AC3).

    ``failure-log``: the writer's dual write recorded the row as not emitted.
    ``journal-dead-letter``: the hook-emit drainer evicted, dropped or dead-lettered it.
    ``journal-pending``: it is still queued in the journal, so it has not reached the bus yet.
    ``unexplained``: no evidence names it; the loss happened where nothing records it.
    """

    FAILURE_LOG = "failure-log"
    JOURNAL_DEAD_LETTER = "journal-dead-letter"
    JOURNAL_PENDING = "journal-pending"
    UNEXPLAINED = "unexplained"


class ModelParityExplainedRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    loss_class: EnumParityLossClass
    detail: str = ""


class ModelParityExplainEvidence(BaseModel):
    """What the emitting host's state directory says about each row id, by class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pending: dict[str, str] = Field(default_factory=dict)
    dead_letter: dict[str, str] = Field(default_factory=dict)
    failure_log: dict[str, str] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict)


class ModelParityExplain(BaseModel):
    """The classification of every row missing from the projection, with counts per class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_log: int = 0
    journal_dead_letter: int = 0
    journal_pending: int = 0
    unexplained: int = 0
    evidence: dict[str, str] = Field(
        default_factory=dict,
        description="Each evidence source read and its path, or why it could not be read.",
    )
    rows: tuple[ModelParityExplainedRow, ...] = ()


class ModelWorkLedgerParityReport(BaseModel):
    """Exact parity requires complete row text and relationship evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_since: datetime
    window_until: datetime
    file_rows: int = Field(
        ..., description="Canonical rows in the file within the window."
    )
    projection_rows: int
    unemittable_rows: int = Field(
        ..., description="File rows with a legacy or tool-internal type, never emitted."
    )
    unemittable_types: dict[str, int] = Field(default_factory=dict)
    state_entities_compared: int
    rows_text_compared: int = Field(
        default=0,
        description="Rows in the window present in both file and projection whose text was compared.",
    )
    relations_compared: int = Field(
        default=0, description="Entities whose relationship closure was compared."
    )
    state_horizon: datetime | None = Field(
        default=None, description="Latest file row folded for the state comparison."
    )
    state_after_horizon: tuple[str, ...] = Field(
        default=(),
        description="Entities whose projected value is later than state_horizon: the file cannot establish it, so it is counted apart, never a mismatch.",
    )
    mismatches: tuple[ModelParityMismatch, ...] = ()
    projection_sources: dict[str, int] = Field(
        default_factory=dict,
        description="Projected rows in the window per source value (OMN-20536).",
    )
    explain: ModelParityExplain | None = Field(
        default=None, description="Present only when the check ran with --explain."
    )

    @property
    def exact(self) -> bool:
        return (
            not self.mismatches
            and self.file_rows > 0
            and self.rows_text_compared == self.file_rows == self.projection_rows
            and self.relations_compared == self.state_entities_compared
        )


__all__: list[str] = [
    "EnumParityLossClass",
    "EnumParityMismatchKind",
    "ModelParityExplain",
    "ModelParityExplainEvidence",
    "ModelParityExplainedRow",
    "ModelParityMismatch",
    "ModelWorkLedgerParityReport",
]
