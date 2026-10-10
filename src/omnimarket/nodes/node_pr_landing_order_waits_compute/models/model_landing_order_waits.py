# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request and result of the landing controller's merge-order wait read.

A lane that cannot land a PR until another PR merges ends with a TERMINAL ``outcome=waiting_order``.
The request carries the ledger rows already parsed, the open PRs the controller may act on and what the
watcher state says of each predecessor; the result says which PRs are still waiting and which waits fired.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow

PredecessorState = Literal["open", "merged", "closed", "unknown"]


class ModelLandingOrderWaitPr(BaseModel):
    """An open PR the controller may act on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[^#\s]+#\d+$", description="repo#n.")
    head_sha: str = Field(
        default="", description="The PR's current head, '' when the watcher has none."
    )


class ModelLandingOrderWaitsRequest(BaseModel):
    """One tick's ledger rows, open PRs and predecessor states."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime = Field(description="The tick's clock, timezone-aware.")
    window_days: float = Field(
        default=7.0, gt=0, description="A TERMINAL older than this is not read."
    )
    rows: tuple[ModelLandingLedgerRow, ...] = Field(
        description="The window's rows in ledger order, none newer than now."
    )
    prs: tuple[ModelLandingOrderWaitPr, ...] = Field(
        description="The open PRs the controller may act on."
    )
    predecessor_states: dict[str, PredecessorState] = Field(
        default_factory=dict,
        description=(
            "Lowercase repo#n -> the watcher's state of that PR. A predecessor absent here reads "
            "'unknown' and keeps its dependant waiting."
        ),
    )

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return value


class ModelLandingOrderWait(BaseModel):
    """A merge-order wait one PR is under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parents: tuple[str, ...] = Field(description="The PRs it waits on, repo#n.")
    ts: str = Field(description="The TERMINAL that began the wait.")
    lane: str
    fired: str = Field(description="Why the wait fired, '' while it holds.")
    state: str = Field(description="What the wait still waits on, '' once fired.")
    head: str = Field(description="The head the TERMINAL named, else the PR's head.")


class ModelLandingOrderWaitsResult(BaseModel):
    """The merge-order waits of one tick, keyed by lowercase repo#n."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    waits_open: dict[str, ModelLandingOrderWait]
    waits_fired: dict[str, ModelLandingOrderWait]
    msg_after_wait: tuple[str, ...] = Field(
        description="Fired waits a non-watcher MSG row named after the wait began."
    )


__all__: list[str] = [
    "ModelLandingOrderWait",
    "ModelLandingOrderWaitPr",
    "ModelLandingOrderWaitsRequest",
    "ModelLandingOrderWaitsResult",
    "PredecessorState",
]
