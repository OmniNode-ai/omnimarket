# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall rule's snapshot rows, thresholds and result (OMN-21880)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    model_validator,
)

from omnimarket.models.liveness.model_automation_liveness import NonEmptyStr


class EnumStallPhase(StrEnum):
    """Where a stall stands at the latest snapshot."""

    #: Red on the share of open pull requests, inside the exception time.
    OPEN = "open"
    #: Past the exception time: one exception row, one cause lane, no more repair workers.
    EXCEPTION_DUE = "exception_due"
    #: Past the question time with no eligible repair: one question to the operator.
    QUESTION_DUE = "question_due"
    #: The context stayed clear for the overlay's number of snapshots.
    CLOSED = "closed"


class ModelLandingStallThresholds(BaseModel):
    """The stall rule's thresholds. They come from the node's overlay; no value has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: A context is red enough when it is red on at least this percent of the open pull requests.
    share_percent: int = Field(ge=1, le=100)
    #: Fewest open pull requests that can open a stall.
    min_open_prs: int = Field(ge=1)
    #: Minutes of an open stall before the exception row and the cause lane.
    exception_minutes: int = Field(ge=1)
    #: Minutes of an open stall before the one question, when no repair is eligible.
    question_minutes: int = Field(ge=1)
    #: Consecutive clear snapshots that close a stall.
    clear_snapshots: int = Field(ge=1)
    #: Rollup contexts that are red wherever any other check is; they name no cause.
    aggregate_contexts: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def _question_after_exception(self) -> Self:
        if self.question_minutes < self.exception_minutes:
            raise ValueError("the question comes after the exception row")
        return self


class ModelRepoContextSnapshot(BaseModel):
    """One reading of a repository: its open pull requests and how many are red per context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: AwareDatetime
    #: ``owner/repo``.
    repo: NonEmptyStr
    open_prs: NonNegativeInt
    #: Context name to the number of open pull requests red on it. Absent means none.
    red_by_context: dict[NonEmptyStr, NonNegativeInt] = Field(default_factory=dict)
    #: Where the reading came from, for the reader of a fixture; not read by the rule.
    source: str = ""

    @model_validator(mode="after")
    def _red_within_open(self) -> Self:
        over = sorted(c for c, n in self.red_by_context.items() if n > self.open_prs)
        if over:
            raise ValueError(f"red on more pull requests than are open: {over}")
        return self


class ModelStallContextRef(BaseModel):
    """One required context of one repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: NonEmptyStr
    context: NonEmptyStr


class ModelLandingStallOpenRequest(BaseModel):
    """Snapshot rows in a window, the thresholds, and where a repair is eligible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshots: tuple[ModelRepoContextSnapshot, ...] = ()
    thresholds: ModelLandingStallThresholds
    #: Contexts with an eligible repair: their stall is not put to the operator as a question.
    eligible_repairs: tuple[ModelStallContextRef, ...] = ()


class ModelContextStall(BaseModel):
    """One stall of one required context: one per episode, however many snapshots read it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: NonEmptyStr
    context: NonEmptyStr
    phase: EnumStallPhase
    opened_at: AwareDatetime
    exception_due_at: AwareDatetime
    question_due_at: AwareDatetime
    closed_at: AwareDatetime | None = None
    #: Red and open pull requests when the stall opened.
    opened_red_prs: NonNegativeInt
    opened_open_prs: NonNegativeInt
    #: Red and open pull requests at the latest snapshot that read the stall.
    red_prs: NonNegativeInt
    open_prs: NonNegativeInt
    #: The latest snapshot of the repository the phase was judged at.
    evaluated_at: AwareDatetime


class ModelLandingStallOpenResult(BaseModel):
    """Every stall the snapshots opened, open or closed, oldest first per context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stalls: tuple[ModelContextStall, ...] = ()


__all__ = [
    "EnumStallPhase",
    "ModelContextStall",
    "ModelLandingStallOpenRequest",
    "ModelLandingStallOpenResult",
    "ModelLandingStallThresholds",
    "ModelRepoContextSnapshot",
    "ModelStallContextRef",
]
