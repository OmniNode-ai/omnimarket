# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the lab-fill enumerate operation (OMN-23250).

The enumerate read has four stages: plan-stalled, ruling-pending and lane-resend each
produce a list of ``(ticket, evidence)`` pairs, and source-tickets resolves every listed
ticket against Linear and the PR watcher. Every stage has its own budget floor, so a slow
stage cannot leave the last one without time, and a stage that is cut marks only itself
stale: the lists the other stages read are returned.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")

SOURCE_KINDS: tuple[str, ...] = ("plan-stalled", "ruling-pending", "lane-resend")
TICKETS_STAGE = "source-tickets"
STAGES: tuple[str, ...] = (*SOURCE_KINDS, TICKETS_STAGE)
TICKET_PATTERN = r"^OMN-[0-9]+$"

SourceState = Literal["ok", "empty", "stale", "unreadable"]


class ModelLabFillSourcesRequest(BaseModel):
    """One enumerate read. Every bound arrives from the caller's overlay; none is set here."""

    model_config = _FROZEN

    budget_s: float = Field(gt=0, description="The whole read's budget in seconds.")
    stage_floor_s: float = Field(
        ge=0,
        description="Seconds every stage is guaranteed: a stage may spend only what is left "
        "after the floors of the stages behind it are set aside.",
    )
    stage_cap_s: Mapping[str, float] = Field(
        description="Most seconds one stage may take, by stage name; a stage with no entry has no cap."
    )
    plans_copy_max_age_min: float = Field(
        gt=0,
        description="A saved plans-today read younger than this is used with no re-read of the ledger.",
    )
    plans_copy_stale_max_age_min: float = Field(
        ge=0,
        description="When the rebuild is cut, a saved copy up to this old is served and the source marked stale; "
        "0 serves none.",
    )
    sources: tuple[str, ...] = Field(
        description="The list sources to read, from plan-stalled, ruling-pending, lane-resend."
    )

    @model_validator(mode="after")
    def _check(self) -> ModelLabFillSourcesRequest:
        unknown = [s for s in self.sources if s not in SOURCE_KINDS]
        if unknown:
            raise ValueError(f"unknown source(s): {', '.join(unknown)}")
        bad_caps = [k for k in self.stage_cap_s if k not in STAGES]
        if bad_caps:
            raise ValueError(f"unknown stage cap(s): {', '.join(bad_caps)}")
        needed = self.stage_floor_s * (len(self.sources) + 1)
        if needed > self.budget_s:
            raise ValueError(
                f"budget_s {self.budget_s} cannot hold {len(self.sources) + 1} floors of {self.stage_floor_s}"
            )
        return self


class ModelLabFillSourceRead(BaseModel):
    """One stage's outcome."""

    model_config = _FROZEN

    stage: str
    state: SourceState
    tickets: tuple[str, ...] = ()
    detail: str = ""
    budget_s: float
    elapsed_s: float
    from_copy_age_min: float | None = None


class ModelLabFillSourceCandidate(BaseModel):
    """A listed ticket, resolved when source-tickets read it."""

    model_config = _FROZEN

    kind: str
    ticket: str = Field(pattern=TICKET_PATTERN)
    evidence: str
    resolved: bool
    title: str = ""
    state_type: str = ""


class ModelLabFillSourceDrop(BaseModel):
    """How many tickets of one source a reason dropped."""

    model_config = _FROZEN

    kind: str
    reason: str
    count: int = Field(ge=1)


class ModelLabFillSourcesResult(BaseModel):
    """The lists every stage read, the candidates left after the drops, and the stages that were cut."""

    model_config = _FROZEN

    reads: tuple[ModelLabFillSourceRead, ...]
    candidates: tuple[ModelLabFillSourceCandidate, ...]
    dropped: tuple[ModelLabFillSourceDrop, ...] = ()
    stale: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    elapsed_s: float
