# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the review finding policy (OMN-17427)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnumFindingDisposition(StrEnum):
    """Why a blocking finding was taken out of the blocking set."""

    NIT = "nit"
    PRE_EXISTING = "pre_existing"
    NO_EVIDENCE = "no_evidence"
    REPEAT = "repeat"
    ALREADY_REVIEWED = "already_reviewed"


class ModelReviewRoundState(BaseModel):
    """What the gate remembers of a pull request between its runs.

    ``posted`` holds the fingerprints of every blocking finding shown on the
    pull request through ``head``; ``posted_before`` holds those shown before
    ``head`` was first reviewed, so a rerun of one head does not read its own
    findings as repeats.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    round: int = Field(
        ge=1, le=10_000, description="Review rounds on this pull request."
    )
    head: str = Field(
        min_length=7, max_length=64, description="Head the round reviewed."
    )
    previous_head: str | None = Field(
        default=None, description="Head of the round before this one."
    )
    posted: tuple[str, ...] = Field(
        default=(), max_length=5_000, description="Fingerprints posted through head."
    )
    posted_before: tuple[str, ...] = Field(
        default=(), max_length=5_000, description="Fingerprints posted before head."
    )


class ModelFindingDrop(BaseModel):
    """One blocking finding that policy removed from the blocking set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str = Field(description="Path the finding was reported against.")
    line_start: int = Field(description="Line the finding was reported at.")
    disposition: EnumFindingDisposition = Field(description="The class it fell in.")
    reason: str = Field(description="Why it cannot block, naming the evidence read.")
    fingerprint: str = Field(description="Stable identity of the finding.")
    message: str = Field(description="The finding's representative message.")


class ModelReviewFindingPolicyRequest(BaseModel):
    """The reviewer's result, the commits it spans and the gate's memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: dict[str, Any] = Field(
        description="The reviewer's ModelMultiReviewResult document, as parsed JSON."
    )
    head_sha: str = Field(min_length=7, description="The pull request head.")
    base_sha: str = Field(min_length=7, description="The pull request base commit.")
    prior_state: ModelReviewRoundState | None = Field(
        default=None, description="State left by the previous run, if any."
    )


class ModelReviewFindingPolicyResult(BaseModel):
    """The result document with only introduced, evidenced findings blocking."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: dict[str, Any] = Field(
        description="The reviewer's document; unchanged when nothing was dropped."
    )
    dropped: tuple[ModelFindingDrop, ...] = Field(
        default=(), description="Blocking findings removed, each with its reason."
    )
    kept: int = Field(default=0, description="Blocking findings that still block.")
    state: ModelReviewRoundState | None = Field(
        default=None, description="State for the next run; None when not computable."
    )
    kept_messages: tuple[str, ...] = Field(
        default=(), description="Title line of each finding that still blocks."
    )


__all__: list[str] = [
    "EnumFindingDisposition",
    "ModelFindingDrop",
    "ModelReviewFindingPolicyRequest",
    "ModelReviewFindingPolicyResult",
    "ModelReviewRoundState",
]
