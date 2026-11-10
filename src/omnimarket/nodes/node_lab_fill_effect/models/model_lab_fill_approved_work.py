# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the approved-work enumerate operation (OMN-23250).

The read moved out of the frozen lab-fill script: the declared approved-work file is validated
row by row, then each valid row is judged against the PR watcher, the merged history and Linear.
Each fact stage has its own budget floor, and a stage that is cut or unreadable is named: the
read then answers ``stale`` or ``unreadable`` with no candidates, never an empty list.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")

APPROVED_STAGES: tuple[str, ...] = ("pr-facts", "merged-history", "approved-states")
ApprovedReadState = Literal["ok", "empty", "stale", "unreadable"]


class ModelLabFillApprovedWorkRequest(BaseModel):
    """One approved-work read. Every bound and list arrives from the caller's overlay."""

    model_config = _FROZEN

    path: str = Field(min_length=1, description="The declared approved-work file.")
    kinds: tuple[str, ...] = Field(
        min_length=1,
        description="Row kinds that count; another kind reads as the first.",
    )
    done_bases: tuple[str, ...] = Field(
        description="Values a row's done_basis may take."
    )
    fleet_repos: tuple[str, ...] = Field(
        description="Repositories a ticket's Repo line may declare."
    )
    max_rows: int = Field(gt=0, description="A list longer than this is unreadable.")
    budget_s: float = Field(gt=0, description="The whole read's budget in seconds.")
    stage_floor_s: float = Field(
        ge=0, description="Seconds every fact stage is guaranteed."
    )


class ModelLabFillApprovedCandidate(BaseModel):
    """A valid approved row with the facts selection judges it on, and why it is skipped, if it is."""

    model_config = _FROZEN

    row: dict[str, object]
    row_hash: str
    title: str
    approved_skip: str = ""
    assignee_id: str = ""
    ticket_updated_at: str = ""
    ticket_text: str = ""
    ticket_blockers: tuple[str, ...] = ()
    ticket_blockers_done: tuple[str, ...] = ()
    acceptance_verified: tuple[str, ...] = ()
    ticket_prs: tuple[str, ...] = ()
    merged_implementations: tuple[str, ...] = ()
    declared_repo: str = ""
    prerequisites_done: tuple[str, ...] = ()


class ModelLabFillApprovedWorkResult(BaseModel):
    """The read's state, the rows it counted by kind, and the candidates."""

    model_config = _FROZEN

    state: ApprovedReadState
    failed_stage: str = ""
    detail: str = ""
    seen: dict[str, int] = Field(default_factory=dict)
    invalid: dict[str, int] = Field(default_factory=dict)
    candidates: tuple[ModelLabFillApprovedCandidate, ...] = ()
    elapsed_s: float = 0.0
