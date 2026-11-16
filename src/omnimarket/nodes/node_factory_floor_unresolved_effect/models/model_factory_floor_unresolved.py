# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the factory floor unresolved step (OMN-19998)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelFactoryFloorUnresolvedRead(BaseModel):
    """One read of the ledger reader across the live ledger and every archive roll, filtered by kind and a text match, with no age limit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    grep: str = Field(min_length=1)


class ModelFactoryFloorUnresolvedOverlay(BaseModel):
    """The deployment's reads and budgets: which reads, from which row source, how many run together, how long one may take, and the margin the record keeps past the window's edge. There is no packaged default for any of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reads: tuple[ModelFactoryFloorUnresolvedRead, ...] = Field(min_length=1)
    # The ledger reader's ``--source`` for every read (file, db or compare; the reader refuses another).
    ledger_source: str = Field(min_length=1)
    read_workers: int = Field(gt=0)
    read_timeout_seconds: int = Field(gt=0)
    overlap_hours: int = Field(gt=0)


class ModelFactoryFloorUnresolvedRequest(BaseModel):
    """One pass: the time it reads at, the plugin root the ledger reader lives under, or a rows file that stands in for the ledger and its archive rolls (tests and replays)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: datetime
    plugin_root: str | None = None
    stand_in_file: str | None = None

    @field_validator("read_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("read_at must carry a UTC offset")
        return value
