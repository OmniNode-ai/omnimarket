# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger source entries: a ruling row whole, with every correction that cites it.

``ModelLedgerSourceEntry`` is a ``ModelContextSourceEntry`` (so the core hash and
verbatim invariants hold) whose ``text`` is the row's ``raw_row`` exactly, never
the projection's 500-character display detail. ``corrected_by`` carries each
correcting row's full text and stamp, so the entry cannot be rendered without
them. When a correction types the operator's words as ``operator_span`` into the
row's first quoted cell, ``operator_words`` is exactly that span and
``recorded_framing`` the rest of the cell (the text before the span, then the
text after it), labelled ``FRAMING_LABEL``. A correction without the typed span
labels the entry ``CORRECTED_LABEL``.
"""

from __future__ import annotations

from typing import Self

from omnibase_core.enums.enum_context_supersession_status import (
    EnumContextSupersessionStatus,
)
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

FRAMING_LABEL = "recorded framing, not operator words"
CORRECTED_LABEL = "corrected, see correction"

_MAY_CARRY_CORRECTIONS = frozenset(
    {
        EnumContextSupersessionStatus.CORRECTED,
        EnumContextSupersessionStatus.SUPERSEDED,
    }
)

__all__ = [
    "CORRECTED_LABEL",
    "FRAMING_LABEL",
    "ModelLedgerCorrection",
    "ModelLedgerSourceEntry",
    "ModelLedgerSourceResult",
]


class ModelLedgerCorrection(BaseModel):
    """One CORRECTION row that cites the entry's row, carried whole."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    row_id: str = Field(min_length=1)
    row_stamp: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    text: str = Field(min_length=1, description="The correction's raw_row, whole")
    operator_span: str | None = Field(
        default=None, description="The correction's operator_span cell, as written"
    )


class ModelLedgerSourceEntry(ModelContextSourceEntry):
    """A RULING or OPERATOR-CONSENT row as a context-source entry."""

    row_id: str = Field(min_length=1)
    row_type: str = Field(min_length=1)
    row_stamp: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    corrected_by: tuple[ModelLedgerCorrection, ...] = ()
    operator_words: str | None = None
    recorded_framing: tuple[str, str] | None = None
    framing_label: str | None = None

    @model_validator(mode="after")
    def _corrections_mark_the_entry(self) -> Self:
        status = self.supersession_status
        if self.corrected_by and status not in _MAY_CARRY_CORRECTIONS:
            raise ValueError(
                "an entry with corrected_by must be supersession_status corrected"
                " (or superseded)"
            )
        if status is EnumContextSupersessionStatus.CORRECTED and not self.corrected_by:
            raise ValueError(
                "a corrected entry must carry the corrections in corrected_by"
            )
        if self.operator_words is not None:
            if self.framing_label != FRAMING_LABEL or self.recorded_framing is None:
                raise ValueError(
                    "operator_words needs recorded_framing labelled as framing"
                )
            if not any(c.operator_span for c in self.corrected_by):
                raise ValueError(
                    "operator_words needs a correction carrying operator_span"
                )
        elif self.recorded_framing is not None:
            raise ValueError("recorded_framing without operator_words")
        return self


class ModelLedgerSourceResult(ModelContextSourceResult):
    """``ModelContextSourceResult`` whose entries are ledger entries."""

    entries: tuple[ModelLedgerSourceEntry, ...] = ()
