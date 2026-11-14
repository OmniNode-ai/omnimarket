# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fenced flip of the ledger serve from the file to the bus (OMN-23154)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumLedgerAppendMode(StrEnum):
    """Where an accepted row lands: the file, or the bus store with a ledger_seq."""

    FILE = "file"
    # The handover: a new command is answered with a retry receipt while in-flight appends drain.
    FENCED = "fenced"
    BUS = "bus"


class ModelBusAppend(BaseModel):
    """One request's rows as the bus store holds them: the stamped text and the sequences it was given."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    rows: str = Field(min_length=1)
    ledger_seqs: tuple[int, ...] = Field(min_length=1)


class ModelFlipResult(BaseModel):
    """The outcome of a flip: the mode the handler stands in and, when it did not flip, why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flipped: bool
    mode: EnumLedgerAppendMode
    reason: str
    in_flight_at_start: int = Field(ge=0)
    # sha256 of the file's text when the flip drained: the file must read the same at a rollback.
    file_sha256: str | None = None


class ModelRollbackResult(BaseModel):
    """The outcome of a rollback to the file judge. It never writes the file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    restored: bool
    mode: EnumLedgerAppendMode
    reason: str
    # Rows the bus accepted after the flip: they stay on the bus and are not written into the file.
    bus_rows_not_in_file: int = Field(ge=0)
    file_unchanged_since_flip: bool | None = None


__all__: list[str] = [
    "EnumLedgerAppendMode",
    "ModelBusAppend",
    "ModelFlipResult",
    "ModelRollbackResult",
]
