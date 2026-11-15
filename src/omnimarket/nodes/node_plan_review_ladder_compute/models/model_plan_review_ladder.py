# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request, result and settings of the plan review ladder decision.

The request carries the plan's first-slice verdict (the same enum the
first-slice check returns), its review history as a tuple of rounds, and the
plan's later rungs in order. A round names its scope and, for a later rung,
which rung it reviewed; only a later rung round carries a rung. The result is
the next move: the whole-plan rounds stop once no blocking finding is open,
then the first slice alone is reviewed until a round is clean, then each later
rung when it is next. ``open_blocking`` counts the open blocking findings of
the last round. Pure: no clock, no I/O, no model.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.enum_plan_first_slice_verdict import (
    EnumPlanFirstSliceVerdict,
)


class EnumPlanReviewScope(StrEnum):
    """What one review round covered."""

    FULL_PLAN = "full_plan"
    FIRST_SLICE = "first_slice"
    LATER_RUNG = "later_rung"


class EnumPlanReviewMove(StrEnum):
    """The next move the ladder makes on a plan in review."""

    RETURN_TO_AUTHOR = "return_to_author"
    FULL_PLAN_ROUND = "full_plan_round"
    STOP_FULL_PLAN_REVIEW_FIRST_SLICE = "stop_full_plan_review_first_slice"
    FIRST_SLICE_ROUND = "first_slice_round"
    BUILD_FIRST_SLICE = "build_first_slice"
    RUNG_ROUND = "rung_round"
    PLAN_COMPLETE = "plan_complete"


class ModelPlanReviewFinding(BaseModel):
    """One finding of a review round, and whether it is still open."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(
        description="The finding's kind (compared against blocking_kinds)."
    )
    open: bool = Field(description="True while the finding is not resolved.")


class ModelPlanReviewRound(BaseModel):
    """One review round: what it covered and what it found."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: EnumPlanReviewScope = Field(
        description="full_plan, first_slice or later_rung."
    )
    rung: str | None = Field(
        default=None,
        description=(
            "The later rung the round reviewed; set for a later_rung round only."
        ),
    )
    findings: tuple[ModelPlanReviewFinding, ...] = Field(
        default=(),
        description="The round's findings, in the order the reviewer reported them.",
    )

    @model_validator(mode="after")
    def _rung_matches_scope(self) -> ModelPlanReviewRound:
        """A later_rung round names its rung; no other round carries one."""
        if self.scope is EnumPlanReviewScope.LATER_RUNG:
            if not self.rung:
                raise ValueError("a later_rung round names its rung")
        elif self.rung is not None:
            raise ValueError("only a later_rung round carries a rung")
        return self


class ModelPlanReviewLadderSettings(BaseModel):
    """The finding kinds that keep a whole-plan round from closing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    blocking_kinds: tuple[str, ...] = Field(
        description=(
            "Finding kinds (compared as written) that block a whole-plan round "
            "from closing; every other kind is non-blocking."
        ),
        min_length=1,
    )


class ModelPlanReviewLadderRequest(BaseModel):
    """One plan in review, its history and the settings in force."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    first_slice_verdict: EnumPlanFirstSliceVerdict = Field(
        description=(
            "The plan's first-slice verdict (the first-slice check's enum); "
            "anything but ok returns the plan to its author."
        ),
    )
    rounds: tuple[ModelPlanReviewRound, ...] = Field(
        default=(),
        description="The plan's review history, in the order the rounds ran.",
    )
    later_rungs: tuple[str, ...] = Field(
        default=(),
        description="The plan's later rungs in ladder order, as the plan names them.",
    )
    settings: ModelPlanReviewLadderSettings | None = Field(
        default=None,
        description=(
            "The blocking finding kinds. Absent: the handler's injected settings, "
            "else the overlay's (the packaged contract's config section when no "
            "overlay is configured)."
        ),
    )


class ModelPlanReviewLadderResult(BaseModel):
    """The ladder's next move on a plan in review, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    move: EnumPlanReviewMove = Field(
        description=(
            "return_to_author, full_plan_round, stop_full_plan_review_first_slice, "
            "first_slice_round, build_first_slice, rung_round or plan_complete."
        ),
    )
    next_scope: EnumPlanReviewScope | None = Field(
        description=("The scope the next round covers, or None when no round follows."),
    )
    next_rung: str | None = Field(
        description=(
            "The later rung the next round reviews, or None when no rung is next."
        ),
    )
    open_blocking: int = Field(
        description=(
            "The last round's open blocking finding count; 0 when no round ran."
        ),
    )
    rule: str = Field(description="The stable id of the rule the decision applied.")
    detail: str = Field(
        description="One plain sentence naming the move and its reason."
    )


__all__: list[str] = [
    "EnumPlanReviewMove",
    "EnumPlanReviewScope",
    "ModelPlanReviewFinding",
    "ModelPlanReviewLadderRequest",
    "ModelPlanReviewLadderResult",
    "ModelPlanReviewLadderSettings",
    "ModelPlanReviewRound",
]
