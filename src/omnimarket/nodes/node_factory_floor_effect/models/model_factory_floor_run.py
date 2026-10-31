# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One factory floor run: the command line's flags and what the run wrote."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class ModelFactoryFloorRunRequest(BaseModel):
    """The flags of one run. ``None`` takes the contract's environment names and the overlay.

    ``plan_path_contains`` empty reads the deployment overlay's selection; ``now`` ``None`` reads the clock
    (the one place the system clock is read); ``ledger_rows_file`` ``None`` reads the ledger reader.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path_contains: tuple[str, ...] = ()
    now: datetime | None = None
    ledger_rows_file: str | None = None
    home: str | None = None
    kb: str | None = None

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("now must carry a UTC offset")
        return value


class ModelFactoryFloorRunResult(BaseModel):
    """Where the JSON was written and what it holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_path: str
    lines: int
    andon_lines: tuple[str, ...]
