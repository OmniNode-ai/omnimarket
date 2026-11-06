# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab-proof profiles, receipt rows and the pure merge admission rule.

The caller supplies the factory ticket manifest, repository profiles and
receipt projection; the decision owns no repository or profile selection.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    PR_KEY_PATTERN,
    REPO_PATTERN,
    SHA_PATTERN,
)


class EnumLandingLabProofResult(StrEnum):
    """The result recorded for the lab-proof run."""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class EnumLandingNegativeControlResult(StrEnum):
    """The outcome recorded for the negative control run."""

    FAIL = "FAIL"
    PASS = "PASS"


class EnumLandingLabProofReason(StrEnum):
    """Why a factory PR's lab proof does not admit its head."""

    ABSENT = "absent"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    STALE_HEAD = "stale_head"
    CONTROL_NOT_FAILING = "control_not_failing"
    PROFILE_MISSING = "profile_missing"


class ModelLandingLabProofProfile(BaseModel):
    """The repository's declared proof profile and optional negative control."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(
        ..., pattern=REPO_PATTERN, description="The repository this profile admits."
    )
    profile_id: str = Field(
        ..., min_length=1, description="The repository's lab-proof profile identifier."
    )
    profile_version: str = Field(
        ..., min_length=1, description="The required version of the proof profile."
    )
    negative_control_kind: str | None = Field(
        default=None,
        min_length=1,
        description="The negative control that must be recorded failing, if declared.",
    )


class ModelLandingLabProofReceipt(BaseModel):
    """One projected receipt for a PR head under a versioned proof profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(
        ..., min_length=1, description="The projection row's receipt key."
    )
    pr: str = Field(
        ..., pattern=PR_KEY_PATTERN, description="The PR the receipt proves."
    )
    head_sha: str = Field(
        ..., pattern=SHA_PATTERN, description="The exact PR head the run tested."
    )
    profile_id: str = Field(
        ..., min_length=1, description="The lab-proof profile the run used."
    )
    profile_version: str = Field(
        ..., min_length=1, description="The version of the profile the run used."
    )
    result: EnumLandingLabProofResult = Field(
        ..., description="The result recorded for the lab-proof run."
    )
    negative_control_kind: str | None = Field(
        default=None, description="The kind of negative control the run recorded."
    )
    negative_control_result: EnumLandingNegativeControlResult | None = Field(
        default=None, description="The outcome recorded for the negative control run."
    )


def lab_proof_gap(
    *,
    pr: str,
    head_sha: str,
    cited_tickets: tuple[str, ...],
    factory_tickets: tuple[str, ...],
    profiles: tuple[ModelLandingLabProofProfile, ...],
    receipts: tuple[ModelLandingLabProofReceipt, ...],
) -> EnumLandingLabProofReason | None:
    """The one gap holding a factory PR's head, or none when proof admits it."""
    if not set(cited_tickets).intersection(factory_tickets):
        return None
    repo = pr.split("#", 1)[0]
    profile = next((p for p in profiles if p.repo == repo), None)
    if profile is None:
        return EnumLandingLabProofReason.PROFILE_MISSING
    candidates = tuple(
        r
        for r in receipts
        if r.pr == pr
        and r.profile_id == profile.profile_id
        and r.profile_version == profile.profile_version
    )
    if not candidates:
        return EnumLandingLabProofReason.ABSENT
    receipt = next((r for r in candidates if r.head_sha == head_sha), None)
    if receipt is None:
        return EnumLandingLabProofReason.STALE_HEAD
    if receipt.result is EnumLandingLabProofResult.FAIL:
        return EnumLandingLabProofReason.FAIL
    if receipt.result is EnumLandingLabProofResult.INCONCLUSIVE:
        return EnumLandingLabProofReason.INCONCLUSIVE
    if profile.negative_control_kind is not None and (
        receipt.negative_control_kind != profile.negative_control_kind
        or receipt.negative_control_result is not EnumLandingNegativeControlResult.FAIL
    ):
        return EnumLandingLabProofReason.CONTROL_NOT_FAILING
    return None


__all__: list[str] = [
    "EnumLandingLabProofReason",
    "EnumLandingLabProofResult",
    "EnumLandingNegativeControlResult",
    "ModelLandingLabProofProfile",
    "ModelLandingLabProofReceipt",
    "lab_proof_gap",
]
