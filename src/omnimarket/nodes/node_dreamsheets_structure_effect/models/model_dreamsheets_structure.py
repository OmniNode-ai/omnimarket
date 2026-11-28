# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, summary record and result for the DreamSheets structure node.

``ModelDreamsheetsStructureSummary`` mirrors ``summary_version: 1`` of the
record DreamSheets' ``get_structure_summary`` tool emits (spec:
``structure-summary.md`` in the DreamSheets OmniNode kit). Every model forbids
extra keys, so a record from a newer DreamSheets release that adds a key fails
validation instead of being half-read.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnumDreamsheetsStructureStatus(StrEnum):
    """How a run ended."""

    COMPLETED = "completed"
    #: Refused before the binary ran: bad input, input too large, binary hash mismatch.
    REFUSED = "refused"
    #: The binary ran but no valid record came back: open error, timeout, bad output.
    FAILED = "failed"


class ModelDreamsheetsStructureRequest(BaseModel):
    """One document to summarize. The binary and sandbox are the handler's
    configuration, never the caller's: a request names only the document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_path: str = Field(min_length=1)
    correlation_id: str | None = None


class ModelDreamsheetsLargestTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    columns: int = Field(ge=0)
    rows: int = Field(ge=0)
    cells: int = Field(ge=0)


class ModelDreamsheetsCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sql_tables: int = Field(ge=0)
    external_query_tables: int = Field(ge=0)
    dataset_tables: int = Field(ge=0)
    calc_columns: int = Field(ge=0)
    tables_with_cell_formulas: int = Field(ge=0)
    tiles: int = Field(ge=0)
    charts: int = Field(ge=0)
    scripts: int = Field(ge=0)
    custom_functions: int = Field(ge=0)
    comment_threads: int = Field(ge=0)
    connections: int = Field(ge=0)
    ai_notes: bool


class ModelDreamsheetsStructureSummary(BaseModel):
    """The record, ``summary_version: 1``. ``None`` means withheld, never zero."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary_version: Literal[1]
    values_withheld: bool
    tabs: int = Field(ge=0)
    tables: int = Field(ge=0)
    columns: int = Field(ge=0)
    rows: int | None = Field(ge=0)
    cells: int | None = Field(ge=0)
    size_bucket: Literal["empty", "xs", "s", "m", "l"] | None
    largest_table: ModelDreamsheetsLargestTable | None
    capabilities: ModelDreamsheetsCapabilities


class ModelDreamsheetsStructureResult(BaseModel):
    """What one run produced. ``detail`` and ``error_code`` are fixed strings
    chosen by this node or DreamSheets' error codes: never document text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumDreamsheetsStructureStatus
    correlation_id: str | None = None
    summary: ModelDreamsheetsStructureSummary | None = None
    error_code: str | None = None
    detail: str = ""
    input_sha256: str | None = None
    binary_sha256: str | None = None
    #: The release the binary reported in ``serverInfo.version``.
    dreamsheets_version: str | None = None


__all__ = [
    "EnumDreamsheetsStructureStatus",
    "ModelDreamsheetsCapabilities",
    "ModelDreamsheetsLargestTable",
    "ModelDreamsheetsStructureRequest",
    "ModelDreamsheetsStructureResult",
    "ModelDreamsheetsStructureSummary",
]
