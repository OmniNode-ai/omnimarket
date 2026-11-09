# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The board's work list (OMN-23184): every in-scope item that needs help, each with its owner state.

Three classes, each one row on the floor with its members listed and paged: the open, non-draft, in-scope pull
requests that are red or conflicting; the in-scope tickets a lane left stopped; and the required checks that are red
past the overlay's age from a shared cause nobody owns. An item's owner state is computed the way every other row
computes work under way: a live lane on it (or on its ticket), else a landing lease, else a cause lease, else nobody.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

WorklistKind = Literal["pr", "stopped", "shared_red"]
OwnerState = Literal["live_lane", "landing_lease", "cause_lease", "unowned"]
WORKLIST_KINDS: tuple[WorklistKind, ...] = ("pr", "stopped", "shared_red")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelWorklistOverlay(_Frozen):
    """The overlay of the work list: one title per class, the page size, and when a shared red counts."""

    titles: dict[WorklistKind, str]
    page_size: int = Field(gt=0)
    # A required check that is red on two or more open in-scope pull requests of a repository counts as a shared red once
    # the oldest of its red results is this old.
    shared_red_after_s: int = Field(gt=0)
    # A ticket's stopped lane is on the list only when the lane's row names one of these cells (a named follow-up).
    stopped_follow_up_cells: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _all_titles(self) -> ModelWorklistOverlay:
        missing = [k for k in WORKLIST_KINDS if not self.titles.get(k)]
        if missing:
            raise ValueError(
                f"the work list overlay titles every class; missing {missing}"
            )
        return self


class ModelWorklistItemIn(_Frozen):
    """One item as the sources read it, before its owner state is judged."""

    kind: WorklistKind
    # ``repo#n`` for a pull request, the ticket id for a stopped ticket, ``repo:check`` for a shared red.
    key: str = Field(min_length=1)
    title: str
    detail: str = ""
    red_checks: tuple[str, ...] = ()
    # The pull requests the item stands on (itself for a pull request, its members for a shared red).
    prs: tuple[str, ...] = ()
    ticket: str = ""
    age_s: int | None = None
    # Owners the controller's state names: a landing lease on the pull request, a lease on the cause that holds it.
    landing_lease: str = ""
    cause_lease: str = ""
    conflicting: bool = False


class ModelWorklistItem(_Frozen):
    kind: WorklistKind
    key: str
    title: str
    detail: str
    red_checks: tuple[str, ...]
    prs: tuple[str, ...]
    ticket: str
    age_s: int | None
    conflicting: bool
    owner_state: OwnerState
    # The lane or lease that owns it; empty when nobody does.
    owner: str


class ModelWorklistClass(_Frozen):
    """One row per class: the counts, and every member in pages, unowned first and oldest first."""

    kind: WorklistKind
    title: str
    total: int
    unowned: int
    owned: int
    page_size: int
    pages: tuple[tuple[ModelWorklistItem, ...], ...]

    @property
    def first_page(self) -> tuple[ModelWorklistItem, ...]:
        return self.pages[0] if self.pages else ()


__all__ = [
    "WORKLIST_KINDS",
    "ModelWorklistClass",
    "ModelWorklistItem",
    "ModelWorklistItemIn",
    "ModelWorklistOverlay",
    "OwnerState",
    "WorklistKind",
]
