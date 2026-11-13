# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable context-arm facts and decisions for code-eval context scoring (OMN-23629)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.context_pack_orchestrator import (
    ModelContextPackBuilt,
    ModelContextPackRequest,
)
from omnimarket.models.model_factor_arm import (
    EnumArmLabel,
    ModelFactorArm,
)
from omnimarket.models.model_factor_matrix import (
    arm_by_label,
    build_code_eval_context_arms,
)


class EnumContextArmOperation(StrEnum):
    """Operations the context-arm compute node performs."""

    SELECT = "select"
    JUDGE = "judge"


class EnumContextArmDecision(StrEnum):
    """Outcome of deciding one context arm for one code-eval task."""

    NO_PACK_NEEDED = "no_pack_needed"
    REQUEST_PACK = "request_pack"
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    REFUSED = "refused"


class EnumContextArmReason(StrEnum):
    """Reason a context arm decided the way it did."""

    OK = "ok"
    ARM_NOT_IN_OVERLAY = "arm_not_in_overlay"
    NO_LANE_KIND = "no_lane_kind"
    NO_ROOM = "no_room"
    UNEXPECTED_PACK = "unexpected_pack_for_task_alone"
    PACK_NOT_ANSWERED = "pack_not_answered"
    PACK_FAILED = "pack_failed"
    PROFILE_MISMATCH = "profile_mismatch"
    REQUIRED_TIER_MISSING = "required_tier_missing"
    TOO_FEW_TIERS = "too_few_tiers"
    EXEMPLAR_NOT_VERIFIABLE = "exemplar_not_verifiable"
    EXEMPLAR_IS_TASK_MERGE_COMMIT = "exemplar_is_task_merge_commit"
    EXEMPLAR_AFTER_BASE = "exemplar_after_base"
    OVER_INPUT_CEILING = "over_input_ceiling"


class ModelContextArmSpec(BaseModel):
    """One overlay context arm: its profile, factor arm and pinned tiers, and its included-tier floor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str | None = Field(
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Context profile the arm scores; absent when the arm scores the task alone.",
    )
    arm_label: EnumArmLabel = Field(
        description=(
            "Label of the arm in the context-ROI code-eval arms; the arm "
            "owns the arm's factors and required factors."
        ),
    )
    pinned_tiers: tuple[EnumContextFactor, ...] = Field(
        description="Tiers pinned to the task's base revision; empty for a task-alone arm.",
    )
    min_included_tiers: int = Field(
        ge=0,
        description="Fewest tiers the pack must include for the arm to be satisfied.",
    )

    def factor_arm(self) -> ModelFactorArm:
        """The code-eval arm for this label, with its factors and required factors."""
        return arm_by_label(build_code_eval_context_arms(), self.arm_label)

    @model_validator(mode="after")
    def validate_arm_shape(self) -> Self:
        """Pair the profile with its factor requirements, and neither without the other."""
        if self.profile is None:
            if (
                self.factor_arm().factors
                or self.pinned_tiers
                or self.min_included_tiers != 0
            ):
                raise ValueError(
                    "a task-alone arm pins no tiers, requires no tiers and sets no tier floor"
                )
        elif not self.factor_arm().factors:
            raise ValueError("a profiled arm must name at least one factor")
        elif self.min_included_tiers < 1:
            raise ValueError("a profiled arm must name at least one included tier")
        elif not set(self.pinned_tiers) <= set(self.factor_arm().factors):
            raise ValueError("pinned tiers must be a subset of the arm's factors")
        elif not set(self.factor_arm().required_factors) <= set(self.pinned_tiers):
            raise ValueError("required factors must be a subset of the pinned tiers")
        return self


class ModelContextArmOverlay(BaseModel):
    """The deployment values the request carries: the arms, the pack budget and the lane mapping."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context_arms: dict[str, ModelContextArmSpec] = Field(
        min_length=1,
        description="Context arms the deployment names, keyed by arm name.",
    )
    pack_budget_tokens: int = Field(
        gt=0,
        description="Token budget the deployment allows a context pack to carry.",
    )
    archetype_lane_kind: dict[str, str] = Field(
        description="Lane kind each node archetype maps to, for tasks that name no lane kind of their own.",
    )


class ModelCodeEvalTask(BaseModel):
    """The code-eval task the context arm decides context for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1, description="Identifier of the code-eval task.")
    repository: str = Field(
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Repository the task is evaluated in.",
    )
    base_commit: str = Field(
        pattern=r"^[0-9a-f]{40}$",
        description="Base commit the task's answer is judged against.",
    )
    base_commit_time: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        description="UTC committer time of the base commit.",
    )
    merge_commit: str = Field(
        pattern=r"^[0-9a-f]{40}$",
        description="The task's own merge commit, the answer.",
    )
    node_archetype: str | None = Field(
        default=None,
        description="Node archetype the task belongs to; names the overlay's lane-kind mapping entry.",
    )
    lane_kind: str | None = Field(
        default=None,
        description="Lane kind the task is scored in, when the task names one.",
    )


class ModelContextArmBackend(BaseModel):
    """The routed backend's token ceiling and the request it composed without any pack."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(
        min_length=1, description="Identifier of the routed backend."
    )
    max_grounded_input_tokens: int = Field(
        ge=1, description="The backend's grounded-input token ceiling."
    )
    composed_request_tokens: int = Field(
        ge=0,
        description="Token count of the request composed without any pack.",
    )
    token_estimator: str = Field(
        min_length=1,
        description="Tokenizer or conservative estimate used to count the request.",
    )


class ModelCodeEvalContextArmRequest(BaseModel):
    """One context-arm decision: the operation, the task, the backend, the overlay and the pack answer when judged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumContextArmOperation = Field(
        description="Operation the compute node performs for this request."
    )
    correlation_id: str = Field(
        min_length=1,
        description="Correlation identifier for this context-arm decision.",
    )
    arm: str = Field(
        min_length=1, description="Context arm name the decision is about."
    )
    task: ModelCodeEvalTask = Field(
        description="Code-eval task the context arm decides context for."
    )
    backend: ModelContextArmBackend = Field(
        description="Routed backend's ceiling and the request composed without any pack."
    )
    overlay: ModelContextArmOverlay = Field(
        description="Deployment values carried on the request: the arms, their profiles and tiers, the pack budget and the lane mapping.",
    )
    pack_answer: ModelContextPackBuilt | None = Field(
        default=None,
        description="Context-pack orchestrator's answer; present only for the judge operation.",
    )

    @model_validator(mode="after")
    def validate_pack_answer(self) -> Self:
        """A pack answer rides the request only when judging."""
        if self.pack_answer is not None and (
            self.operation is not EnumContextArmOperation.JUDGE
        ):
            raise ValueError("pack_answer is only allowed for the judge operation")
        return self


class ModelCodeEvalContextArmResult(BaseModel):
    """The context-arm decision and the pack facts it was decided over; no pack text rides this result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        description="Correlation identifier for this context-arm decision."
    )
    operation: EnumContextArmOperation = Field(
        description="Operation the decision came from."
    )
    arm: str = Field(description="Context arm name the decision is about.")
    decision: EnumContextArmDecision = Field(
        description="Context-arm decision for this task."
    )
    reason: EnumContextArmReason = Field(
        description="Reason the decision came out the way it did."
    )
    detail: str = Field(description="Factual detail of this decision.")
    unscorable_context: bool = Field(
        description="Whether the context cannot score the task; every not_satisfied decision sets it.",
    )
    pack_request: ModelContextPackRequest | None = Field(
        description="Context-pack request the arm selects; present only when a pack is requested."
    )
    pack_budget_tokens: int | None = Field(
        description="Pack budget the arm bounded, when one was bounded."
    )
    token_estimate: int = Field(
        ge=0,
        description="Token estimate of the pack the arm judged, or of the budget it requested.",
    )
    token_estimate_basis: Literal["none", "budget_ceiling", "included_tiers"] = Field(
        description="What token_estimate counts: nothing, the requested budget ceiling, or the included tiers.",
    )
    included_tiers: tuple[str, ...] = Field(
        default=(),
        description="Factor values of the tiers the judged pack includes, in tier order.",
    )
    excluded_by_budget: tuple[str, ...] = Field(
        default=(),
        description="Factor values the judged pack's budget excluded.",
    )
    unresolved_tiers: tuple[str, ...] = Field(
        default=(),
        description="Factor values of the tiers the judged pack did not include.",
    )
    final_pack_hash: str | None = Field(
        description="Hash of the judged pack; absent when no pack was judged."
    )
    exemplar_ref: str | None = Field(
        description="12-character sha of the exemplar commit the judged pack carried."
    )
