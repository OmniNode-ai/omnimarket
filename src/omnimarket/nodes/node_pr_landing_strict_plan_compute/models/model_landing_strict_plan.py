# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The order of a landing tick's merges and branch updates on the bases that require branches up to date.

The tick's decision names the merges and update-branches it wants; on a base whose branch protection
requires the branch up to date, every merge makes every other head behind, so the tick orders those
actions and defers the surplus updates. This request carries the decision's actions, the live read of
each PR, the base settings the caller has read, the strict-slot memory, the declared cause fixes and the
M4 delegation PRs; the result is the order, the deferred updates and the next strict-slot view.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelStrictAction(BaseModel):
    """One action of the tick's decision, in the decision's order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(
        min_length=1, description="merge, update_branch, dispatch_worker, ..."
    )
    subject: str = Field(
        min_length=1, description="owner/repo#n, or a non-PR subject without #."
    )


class ModelStrictLivePr(BaseModel):
    """The live read of one PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head: str = ""
    base: str = ""
    rollup: str = ""
    state: str = "OPEN"
    merge_state: str = ""
    mergeable: str = ""


class ModelStrictPending(BaseModel):
    """Since when a declared fix's head has had checks pending."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head: str
    since: str


class ModelStrictSlotEntry(BaseModel):
    """The strict-slot memory of one PR: the update it was last given and how its fresh heads fared."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    from_head: str | None = Field(default=None, alias="from")
    fresh: tuple[str, ...] = ()
    strikes: tuple[str, ...] = ()
    yielded: str | None = None
    pending: ModelStrictPending | None = None
    at: str | None = None


class ModelStrictCauseFixClaim(BaseModel):
    """An open cause CLAIM that declares a fix PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(min_length=3)
    lane: str
    cause: str


class ModelLandingStrictPlanRequest(BaseModel):
    """One tick's decision actions and what the ordering reads around them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime = Field(description="The tick's clock, timezone-aware.")
    actions: tuple[ModelStrictAction, ...] = ()
    live: dict[str, ModelStrictLivePr] = Field(
        default_factory=dict,
        description="The live read per watcher key, repo#n; an unread PR is absent.",
    )
    strict_of: dict[str, bool | None] = Field(
        default_factory=dict,
        description="Per repo:base, whether it requires branches up to date; None or absent is unread, ordered like strict.",
    )
    slot_memory: dict[str, ModelStrictSlotEntry] = Field(default_factory=dict)
    fix_prs: tuple[str, ...] = Field(
        default=(), description="PRs a cause lease declares as its fix."
    )
    fix_why: dict[str, str] = Field(default_factory=dict)
    cause_fix_claims: tuple[ModelStrictCauseFixClaim, ...] = ()
    m4: tuple[str, ...] = ()

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return value


class ModelLandingStrictPlanResult(BaseModel):
    """The ordering of the tick's actions and what the next tick remembers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_of: dict[int, str] = Field(
        description="Action index to repo:base, for each ordered action."
    )
    deferred_updates: dict[int, str] = Field(
        description="Update-branch actions that wait for a later tick."
    )
    details: dict[int, str]
    held: dict[int, str] = Field(
        description="Actions held for a declared fix on their base."
    )
    holding: dict[str, tuple[str, ...]] = Field(
        description="Per repo:base, the fixes that hold it."
    )
    fixes: dict[str, str] = Field(description="Each declared fix PR and why.")
    order: tuple[int, ...] = Field(
        description="A permutation of the action indexes to perform in."
    )
    ordered: dict[str, tuple[int, ...]] = Field(
        description="Per ordered base, its action indexes in rank order."
    )
    slot: dict[str, ModelStrictSlotEntry]
    yielded: dict[str, ModelStrictSlotEntry]
    bases_to_read: tuple[str, ...] = Field(
        description="The repo:base keys the plan looked up in strict_of, so a caller can read the unread ones and ask again."
    )
