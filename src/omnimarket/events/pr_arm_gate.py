# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared arm-gate models for the merge-queue governor."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ModelArmCandidate(BaseModel):
    """Genuine, tri-state facts about one merge-intent PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Repo slug, e.g. 'OmniNode-ai/omnimarket'.")
    pr_number: int = Field(..., description="GitHub PR number.")
    is_draft: bool | None = Field(
        default=None,
        description="True/False from a genuine read; None means unknown -> WITHHOLD.",
    )
    coderabbit_unresolved: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Count of unresolved CodeRabbit review threads. None means the "
            "count was never collected -> WITHHOLD (never defaults to 0)."
        ),
    )
    merge_state_status: str | None = Field(
        default=None,
        description="GitHub mergeStateStatus: CLEAN | DIRTY | BLOCKED | BEHIND | UNKNOWN | None.",
    )
    status_checks: str | None = Field(
        default=None,
        description="Positively collected CI rollup: SUCCESS | FAILURE | PENDING | None.",
    )
    occ_companion_verified: bool | None = Field(
        default=None,
        description=(
            "EFFECT-computed fact (live read-back) proving a verified OCC "
            "evidence companion is attached to this PR. Never re-derived here."
        ),
    )
    priority_hint: int = Field(
        default=0,
        description=(
            "Caller-supplied ordering hint for wave-cap selection among "
            "multiple ARM-eligible candidates in one pass; lower arms first."
        ),
    )


class EnumArmDecision(StrEnum):
    """The arm-gate's binary verdict for one PR."""

    ARM = "arm"
    WITHHOLD = "withhold"


class ModelArmGateDecision(BaseModel):
    """Typed ARM/WITHHOLD verdict with machine-readable reasons."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(...)
    pr_number: int = Field(...)
    decision: EnumArmDecision = Field(...)
    withheld_reasons: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Empty iff decision == ARM; every failed/unknown criterion otherwise.",
    )
    priority_score: int = Field(
        default=0,
        description="Echoes ModelArmCandidate.priority_hint for wave-cap ordering.",
    )


class EnumArmActionMode(StrEnum):
    """The merge-queue governor's action mode."""

    REPORT_ONLY = "report_only"
    ENFORCE = "enforce"


class ModelArmGatePolicy(BaseModel):
    """Operator-controlled policy consumed by the arm-gate decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_mode: EnumArmActionMode = Field(
        default=EnumArmActionMode.REPORT_ONLY,
        description="report_only (default, zero mutation) or enforce (opt-in).",
    )
    kill_switch: bool = Field(
        default=True,
        description=(
            "Emergency stop. Defaults ENGAGED (True) — an operator must "
            "explicitly disengage it (set False) in addition to selecting "
            "action_mode=enforce before any PR can arm."
        ),
    )
    wave_cap: int = Field(
        default=3,
        ge=0,
        description=(
            "Maximum number of PRs armed in a single enforce pass, bounding "
            "the blast radius of one sweep. 0 arms nothing regardless of "
            "action_mode."
        ),
    )
    enable_stall_remediation: bool = Field(
        default=False,
        description=(
            "Opt-in flag for merge-queue stall remediation (dequeue + "
            "re-enqueue an already-armed PR to re-mint a stuck merge-group "
            "SHA). This is a SEPARATE operation from readiness-arming an "
            "unarmed PR — it re-mints an already-armed PR's queue entry, so it "
            "is gated by this flag rather than by the arm-gate's per-PR ARM "
            "decision. Still requires action_mode=enforce and a disengaged "
            "kill_switch."
        ),
    )


class ModelArmGateRequest(BaseModel):
    """One candidate PR plus the policy it is evaluated against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: ModelArmCandidate = Field(..., description="Genuine per-PR facts.")
    policy: ModelArmGatePolicy = Field(
        ..., description="Operator-controlled action policy."
    )


__all__ = [
    "EnumArmActionMode",
    "EnumArmDecision",
    "ModelArmCandidate",
    "ModelArmGateDecision",
    "ModelArmGatePolicy",
    "ModelArmGateRequest",
]
