# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelHeadCheckFacts: input of the ``classify_head_checks`` operation.

Everything the classifier needs about one PR head, read beforehand by an
effect: every check-run copy (from the check-runs API, paginated, never the
status rollup), the failed-step facts of the non-green ones, the
change-control companion's state, and the PR's merge state. The operation
itself does no I/O.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_companion_state import (
    COMPANION_STATES_WITH_PR,
    EnumHeadCheckCompanionState,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_pr_merge_state import (
    EnumPrMergeState,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_run import (
    ModelHeadCheckRun,
)

_REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_SHA_PATTERN = r"^[0-9a-f]{40}$"


class ModelHeadCheckFacts(BaseModel):
    """The check facts of one PR head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(
        ..., pattern=_REPOSITORY_PATTERN, description="owner/name of the product repo."
    )
    pr_number: int = Field(..., ge=1, description="Pull request number.")
    head_sha: str = Field(
        ..., pattern=_SHA_PATTERN, description="The head commit the checks ran on."
    )
    base_ref: str = Field(..., min_length=1, description="The PR's base branch.")
    observed_at: datetime = Field(
        ...,
        description=(
            "The moment these facts describe. Every check-run state, the "
            "companion state and the merge state are as of this moment."
        ),
    )
    checks: tuple[ModelHeadCheckRun, ...] = Field(
        ..., description="Every check-run copy at the head."
    )
    companion_state: EnumHeadCheckCompanionState = Field(
        ..., description="The change-control companion's state."
    )
    companion_pr: int | None = Field(
        default=None,
        ge=1,
        description="The companion PR number, whenever a companion exists.",
    )
    companion_decline_reason: str | None = Field(
        default=None, description="The producer's refusal reason, when declined."
    )
    merge_state: EnumPrMergeState = Field(
        ..., description="GitHub's mergeable_state for the PR."
    )
    base_requires_up_to_date: bool = Field(
        ...,
        description="Whether the base requires the branch to be up to date (strict).",
    )

    @model_validator(mode="after")
    def _consistent(self) -> ModelHeadCheckFacts:
        has_pr = self.companion_state in COMPANION_STATES_WITH_PR
        if has_pr and self.companion_pr is None:
            raise ValueError(
                f"companion_pr is required when companion_state is {self.companion_state}"
            )
        if not has_pr and self.companion_pr is not None:
            raise ValueError(
                f"companion_pr must be empty when companion_state is {self.companion_state}"
            )
        if (
            self.companion_decline_reason is not None
            and self.companion_state is not EnumHeadCheckCompanionState.DECLINED
        ):
            raise ValueError(
                "companion_decline_reason is set on a companion not declined"
            )
        ids = [check.check_run_id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("two check-run copies share one check_run_id")
        return self


__all__: list[str] = ["ModelHeadCheckFacts"]
