# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing workflow's durable row for one PR (the ``state_io`` payload).

Fields follow plan section 5.1: the head, the base, the tickets, draft and
held, the companion (its PR and status), whether the product body carries the
stamp, GitHub's merge state, how the PR is armed, the budgets, a per-key
``seq`` and the time the current state was entered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_arm_method import (
    EnumPrLandingArmMethod,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_companion_status import (
    EnumPrLandingCompanionStatus,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_observation import (
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
        description="none, open, conflicting, merged, closed or declined.",
    )

    @model_validator(mode="after")
    def _a_bound_status_names_its_pr(self) -> Self:
        unbound = (
            EnumPrLandingCompanionStatus.NONE,
            EnumPrLandingCompanionStatus.DECLINED,
        )
        if self.status not in unbound and self.occ_pr is None:
            msg = f"companion status {self.status.value} needs occ_pr"
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
    companion: ModelPrLandingCompanion = Field(default_factory=ModelPrLandingCompanion)
    stamp_present: bool = Field(default=False)
    merge_state: str | None = Field(
        default=None,
        min_length=1,
        description="GitHub mergeStateStatus as last read, verbatim.",
    )
    armed: EnumPrLandingArmMethod | None = Field(
        default=None, description="None while not armed."
    )
    budgets: ModelPrLandingBudgets = Field(default_factory=ModelPrLandingBudgets)
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
