# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed command and outcome of the runner-group exposure reading writer (OMN-22860)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class ModelExposureReadingCommand(BaseModel):
    """One scheduled reading: the correlation id, the time it is judged at, and whether to write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: datetime
    write: bool


class ModelExposureReadingOutcome(BaseModel):
    """What one reading came to and what happened on the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    host: str
    backend: Literal["inmemory", "github"]
    verdict: EnumReadingVerdict
    outcome: EnumReadingOutcome
    reasons: tuple[str, ...]
    calls: int = Field(ge=0)
    row_key: str
    #: ``written``, ``already-written``, ``not-written`` (a probe) or ``append-failed:<why>``.
    ledger: str
    row: str | None = None

    @property
    def failed(self) -> bool:
        """The reading failed: the group was not read, or the append did not land.

        A NOT_WORKING verdict that was read is a reading, never a failure of the run.
        """
        return self.outcome is not EnumReadingOutcome.READ or self.ledger.startswith(
            "append-failed"
        )


__all__ = ["ModelExposureReadingCommand", "ModelExposureReadingOutcome"]
