# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing workflow's durable row for one PR (the ``state_io`` payload).

Fields follow section 3 of the model-checked revision of plan section 5.1
(revision 1): the head, the base, the tickets, draft and held, the ordering
key of the newest applied snapshot (F1, F2), the companion with the command in
flight (F4, F5), whether the product body carries the stamp, GitHub's merge
state, how the PR is armed (set when the arm intent is written, R4), the
expected run attempt per re-run check (F7), the open-episode counter (F10),
the state-entry generation the completion bound is tied to (R2b), the budgets,
the in-row outbox (F6, F8, F9), a per-key ``seq`` and the time the current
state was entered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing.enum_pr_landing_arm_method import (
    EnumPrLandingArmMethod,
)
from omnimarket.events.pr_landing.enum_pr_landing_companion_status import (
    EnumPrLandingCompanionStatus,
)
from omnimarket.events.pr_landing.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.events.pr_landing.model_pr_landing_check_attempt import (
    ModelPrLandingCheckAttempt,
    unique_checks,
)
from omnimarket.events.pr_landing.model_pr_landing_intent import (
    ModelPrLandingIntent,
)
from omnimarket.events.pr_landing.model_pr_landing_observation import (
    HEAD_SHA_PATTERN,
    REPOSITORY_PATTERN,
    fill_landing_key,
)

# Budgets from plan section 5.1. derive and regenerate are per PR; update-branch
# and re-runs are per head and reset when a new head is observed.
DERIVE_BUDGET = 2
REGENERATE_BUDGET = 3
UPDATE_BRANCH_BUDGET_PER_HEAD = 2
RERUNS_PER_CHECK_PER_HEAD = 1


class ModelPrLandingCompanion(BaseModel):
    """The change-control companion bound to the product PR, if any."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    occ_pr: int | None = Field(
        default=None, ge=1, description="Companion PR number, when one exists."
    )
    status: EnumPrLandingCompanionStatus = Field(
        default=EnumPrLandingCompanionStatus.NONE,
        description=(
            "none, pending, open, conflicting, merged, closed or declined. "
            "Written by a companion outcome in every non-terminal state and in "
            "CLOSED (R1)."
        ),
    )
    command_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "The derive or regenerate command in flight (F4). Outcomes correlate "
            "by it, and an outcome for any other command is dropped (F5)."
        ),
    )

    @model_validator(mode="after")
    def _a_bound_status_names_its_pr(self) -> Self:
        unbound = (
            EnumPrLandingCompanionStatus.NONE,
            EnumPrLandingCompanionStatus.PENDING,
            EnumPrLandingCompanionStatus.DECLINED,
        )
        if self.status not in unbound and self.occ_pr is None:
            msg = f"companion status {self.status.value} needs occ_pr"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _pending_exactly_while_a_command_is_in_flight(self) -> Self:
        # CompTracked (R1): a pending status always has its command in flight,
        # and a completed command never stays pending.
        pending = self.status is EnumPrLandingCompanionStatus.PENDING
        if pending != (self.command_id is not None):
            msg = "command_id is set exactly while the companion status is pending"
            raise ValueError(msg)
        return self


class ModelPrLandingBudgets(BaseModel):
    """Remaining budgets. Spending past zero is refused by the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    derive_left: int = Field(default=DERIVE_BUDGET, ge=0, le=DERIVE_BUDGET)
    regenerate_left: int = Field(default=REGENERATE_BUDGET, ge=0, le=REGENERATE_BUDGET)
    update_branch_left: int = Field(
        default=UPDATE_BRANCH_BUDGET_PER_HEAD,
        ge=0,
        le=UPDATE_BRANCH_BUDGET_PER_HEAD,
        description="Per head; reset on a new head.",
    )
    rerun_checks: tuple[str, ...] = Field(
        default=(),
        description=(
            "Check names already re-run on the current head. One re-run per "
            "(head, check) is the whole budget (safety property P2)."
        ),
    )

    @model_validator(mode="after")
    def _each_check_rerun_once(self) -> Self:
        if len(set(self.rerun_checks)) != len(self.rerun_checks):
            msg = "a check may be re-run at most once per head"
            raise ValueError(msg)
        return self


class ModelPrLandingState(BaseModel):
    """One PR's landing row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(..., ge=1)
    state: EnumPrLandingState = Field(...)
    head_sha: str | None = Field(
        default=None,
        pattern=HEAD_SHA_PATTERN,
        description="The head every non-terminal observation must match.",
    )
    base_ref: str | None = Field(default=None, min_length=1)
    ticket_ids: tuple[str, ...] = Field(default=())
    draft: bool = Field(default=False)
    held: bool = Field(default=False)
    source_seq: int = Field(
        default=0,
        ge=0,
        description=(
            "The ordering key of the newest snapshot the row applied: the "
            "orchestrator's read sequence for the PR (F1, F2)."
        ),
    )
    companion: ModelPrLandingCompanion = Field(default_factory=ModelPrLandingCompanion)
    stamp_present: bool = Field(default=False)
    merge_state: str | None = Field(
        default=None,
        min_length=1,
        description="GitHub mergeStateStatus as last read, verbatim.",
    )
    armed: EnumPrLandingArmMethod | None = Field(
        default=None,
        description=(
            "None while not armed. Set when the arm intent is written, not when "
            "it is confirmed, so a queued, in-flight or confirmed arm all count (R4)."
        ),
    )
    expected_attempts: tuple[ModelPrLandingCheckAttempt, ...] = Field(
        default=(),
        description="Per re-run check, the run attempt the last re-run started (F7).",
    )
    episode: int = Field(
        default=0,
        ge=0,
        description="The open-episode counter, incremented on reopen (F10).",
    )
    state_entry_generation: int = Field(
        default=0,
        ge=0,
        description=(
            "Incremented whenever the state, head_sha or episode changes. A "
            "completion-bound expiry applies only when it carries this value "
            "and this episode (R2b)."
        ),
    )
    budgets: ModelPrLandingBudgets = Field(default_factory=ModelPrLandingBudgets)
    outbox: tuple[ModelPrLandingIntent, ...] = Field(
        default=(),
        description=(
            "Intents written in the same compare-and-set as the row and drained "
            "by the dispatcher through that compare-and-set (F6, F8). Delivery is "
            "at-least-once; effects and consumers deduplicate (F9)."
        ),
    )
    seq: int = Field(
        ...,
        ge=0,
        description="Per-key transition sequence; the projection's ordering authority.",
    )
    entered_state_at: datetime = Field(...)
    landing_key: str = Field(
        ..., description="``owner/repo#123``, the ``state_io`` row key."
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_landing_key(cls, data: object) -> object:
        return fill_landing_key(data)

    @model_validator(mode="after")
    def _each_expected_attempt_names_one_check(self) -> Self:
        unique_checks(self.expected_attempts)
        return self

    @model_validator(mode="after")
    def _armed_only_where_arming_is_legal(self) -> Self:
        # Safety property P1: never armed while draft or held.
        if self.armed is not None and (self.draft or self.held):
            msg = "a draft or held PR cannot be armed"
            raise ValueError(msg)
        return self


__all__: list[str] = [
    "DERIVE_BUDGET",
    "REGENERATE_BUDGET",
    "RERUNS_PER_CHECK_PER_HEAD",
    "UPDATE_BRANCH_BUDGET_PER_HEAD",
    "ModelPrLandingBudgets",
    "ModelPrLandingCompanion",
    "ModelPrLandingState",
]
