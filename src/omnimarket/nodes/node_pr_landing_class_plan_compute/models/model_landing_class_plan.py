# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class actions one landing tick plans: stale CI Summary reruns, retargets and their quota cap.

The per-PR reading that decides which PRs need a class action (a CI Summary left red by cancelled
siblings, a PR based on main where its repository lands on dev, a stale copy of a required context)
is done before the tick's decision; this request carries its outcome, the decision's acted-on
subjects and the sidecar memories, and the result is what the tick performs and remembers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

StaleStep = Literal["rerun", "update_branch", "rerun_run"]
RetargetStep = Literal["retarget", "comment", "wait"]
ClassStep = Literal["rerun", "update_branch", "rerun_run", "retarget", "comment"]


class ModelLandingStaleMemory(BaseModel):
    """The sidecar's ``stale_refresh`` entry of one PR: the steps taken per head, the update-branches so far."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    heads: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    refreshes: int = Field(default=0, ge=0)
    at: str | None = None


class ModelLandingRetargetMemory(BaseModel):
    """The sidecar's ``retarget`` entry of one PR: retargets tried on a head, and whether the author was asked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head: str | None = None
    n: int = Field(default=0, ge=0)
    commented: bool = False
    at: str | None = None


class ModelLandingStaleNow(BaseModel):
    """A PR whose red is only stale this tick, and the step the controller takes on it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    short: str = Field(min_length=3, description="The watcher's key, repo#n.")
    step: StaleStep
    head: str = Field(min_length=1)
    run_id: str | None = Field(
        default=None, description="The superseded run a rerun_run step reruns."
    )

    @model_validator(mode="after")
    def _run_only_for_rerun_run(self) -> ModelLandingStaleNow:
        if (self.step == "rerun_run") != (self.run_id is not None):
            raise ValueError("run_id is required for a rerun_run step and only then")
        return self


class ModelLandingRetargetNow(BaseModel):
    """A PR based on main where its repository lands on dev, and the step for its head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    short: str = Field(min_length=3)
    step: RetargetStep
    head: str = Field(min_length=1)


class ModelLandingPendingGated(BaseModel):
    """A red PR held because these required contexts are still running."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    short: str = Field(min_length=3)
    names: tuple[str, ...]


class ModelLandingClassPlanRequest(BaseModel):
    """One tick's class candidates, the decision's acted-on subjects, the quota cap and the memories."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime = Field(description="The tick's clock, timezone-aware.")
    class_cap: int = Field(ge=0, description="The quota governor's per-tick cap.")
    class_reads: int = Field(
        ge=0, description="What the required-context reads already spent of the cap."
    )
    stale: tuple[ModelLandingStaleNow, ...] = ()
    retarget: tuple[ModelLandingRetargetNow, ...] = ()
    pending_gated: tuple[ModelLandingPendingGated, ...] = ()
    acted: tuple[str, ...] = Field(
        default=(),
        description="The decision's action subjects; a subject with # is a PR the decision acts on itself.",
    )
    m4: tuple[str, ...] = Field(
        default=(), description="The M4 delegation PRs, which take the cap first."
    )
    stale_memory: dict[str, ModelLandingStaleMemory] = Field(default_factory=dict)
    retarget_memory: dict[str, ModelLandingRetargetMemory] = Field(default_factory=dict)
    pr_states: dict[str, str] = Field(
        default_factory=dict,
        description="The watcher's state per PR (OPEN, MERGED, CLOSED); a PR absent here is UNKNOWN.",
    )

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _one_entry_per_pr(self) -> ModelLandingClassPlanRequest:
        for name, entries in (
            ("stale", self.stale),
            ("retarget", self.retarget),
            ("pending_gated", self.pending_gated),
        ):
            shorts = [e.short for e in entries]
            if len(shorts) != len(set(shorts)):
                raise ValueError(f"{name} names a PR twice")
        return self


class ModelLandingClassAction(BaseModel):
    """One class action planned (or deferred) for a PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    short: str
    step: ClassStep
    head: str
    run_id: str | None = None


class ModelLandingClassSummary(BaseModel):
    """The tick receipt's ``landing_classes``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_rerun: tuple[str, ...]
    stale_refresh: tuple[str, ...]
    stale_copy_rerun: tuple[str, ...]
    retarget: tuple[str, ...]
    retarget_ask: tuple[str, ...]
    retarget_wait: tuple[str, ...]
    pending_gated: dict[str, tuple[str, ...]]
    deferred: tuple[str, ...]
    cap: int
    reads: int


class ModelLandingClassPlanResult(BaseModel):
    """What the tick performs, defers and remembers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: tuple[ModelLandingClassAction, ...]
    deferred: tuple[ModelLandingClassAction, ...]
    stale_memory: dict[str, ModelLandingStaleMemory]
    retarget_memory: dict[str, ModelLandingRetargetMemory]
    summary: ModelLandingClassSummary
