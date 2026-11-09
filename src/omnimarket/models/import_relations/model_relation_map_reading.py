# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command and result of the relation map reading: the map's progress as one reading row."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.import_relations.model_dependency_gate import (
    ModelDependencyGateResult,
)
from omnimarket.models.import_relations.model_import_relations import (
    ModelImportRelationReport,
)

ReadingVerdict = Literal["WORKING", "NOT_WORKING", "UNKNOWN"]


class ModelRelationMapReadingCommand(BaseModel):
    """Write the reading row of the relation map for one piece.

    ``report`` is the import relation report; ``None`` means the graph run could not be read,
    which gives an UNKNOWN row and never a silent skip. ``write=False`` computes and appends nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    piece: int = Field(ge=1)
    window_start: datetime
    report: ModelImportRelationReport | None
    #: The dependency gate's verdict over the same report; the row carries its current and frozen counts.
    #: ``None`` reads as ``count=unread`` in the row, never as a zero.
    gate: ModelDependencyGateResult | None = None
    write: bool = True


class ModelRelationMapReadingResult(BaseModel):
    """What the writer did: the verdict it gave, the reason cell and the row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["written", "already-written", "not-written", "failed"]
    verdict: ReadingVerdict
    reason: str
    row: str | None = None
    failure: str | None = None
