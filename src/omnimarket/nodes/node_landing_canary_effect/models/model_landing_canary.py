# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the landing canary: the test pull request, its state, the run and its reading."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class EnumCanaryEnding(StrEnum):
    """How one canary run ended."""

    #: The test pull request merged inside the bound.
    MERGED = "merged"
    #: It merged, but after the bound.
    MERGED_LATE = "merged-late"
    #: It was still open when the bound ran out.
    STALLED = "stalled"
    #: It was closed without merging.
    CLOSED_UNMERGED = "closed-unmerged"
    #: The test pull request could not be opened.
    OPEN_FAILED = "open-failed"
    #: It was opened but its state could not be read at the end.
    STATE_UNREADABLE = "state-unreadable"


class ModelCanaryOpenRequest(BaseModel):
    """What the repository port needs to open one test pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    base: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    nonce: str = Field(min_length=1)
    title: str = Field(min_length=1)


class ModelCanaryOpened(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    head_sha: str = Field(min_length=1)
    #: The instant the pull request began to open, when the repository can say; the run counts its own
    #: instant from before the open call otherwise. Never the instant the create command finished.
    opened_at: datetime | None = None


class ModelCanaryState(BaseModel):
    """The test pull request as the watcher reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    merged: bool
    closed: bool = False
    #: The merge instant when the source gives it; otherwise the run's own reading instant counts.
    merged_at: datetime | None = None


class ModelLandingCanaryCommand(BaseModel):
    """Run the canary once; ``write`` false runs it and appends nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool = True


class ModelLandingCanaryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    host: str
    failure: str | None = None
    verdict: EnumReadingVerdict = EnumReadingVerdict.UNKNOWN
    outcome: EnumReadingOutcome = EnumReadingOutcome.SOURCE_UNREADABLE
    ending: EnumCanaryEnding | None = None
    reasons: tuple[str, ...] = ()
    repo: str = ""
    pr_number: int | None = None
    head_sha: str = ""
    #: Seconds from the pull request opening to its merge (or to the end of the wait when it did not merge).
    elapsed_seconds: int | None = None
    bound_seconds: int | None = None
    row_key: str = ""
    ledger: str = "not-written"
    row: str | None = None

    @property
    def failed(self) -> bool:
        """The run itself failed; a NOT_WORKING verdict that was read is not a failure."""
        return (
            self.status == "failed"
            or self.outcome is not EnumReadingOutcome.READ
            or self.ledger.startswith("append-failed")
        )


__all__ = [
    "EnumCanaryEnding",
    "ModelCanaryOpenRequest",
    "ModelCanaryOpened",
    "ModelCanaryState",
    "ModelLandingCanaryCommand",
    "ModelLandingCanaryResult",
]
