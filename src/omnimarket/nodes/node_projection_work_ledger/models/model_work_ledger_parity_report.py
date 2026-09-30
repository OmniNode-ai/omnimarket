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
    STATE_MISSING_IN_PROJECTION = "state_missing_in_projection"
    STATE_OPENED_AT_DIFFERS = "state_opened_at_differs"
    STATE_CLOSED_AT_DIFFERS = "state_closed_at_differs"


class ModelParityMismatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumParityMismatchKind
    key: str = Field(
        ..., description="row_id for row mismatches, entity_key for state."
    )
    detail: str = ""


class ModelWorkLedgerParityReport(BaseModel):
    """Exact parity means ``mismatches`` is empty and the window held at least one row."""

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
    mismatches: tuple[ModelParityMismatch, ...] = ()

    @property
    def exact(self) -> bool:
        return not self.mismatches and self.file_rows > 0


__all__: list[str] = [
    "EnumParityMismatchKind",
    "ModelParityMismatch",
    "ModelWorkLedgerParityReport",
]
