# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J8 lab-check models: the check runs the landing decision reads and the runner-group snapshot (OMN-22919).

Rebuild plan 4.1, J8 (piece 8 to piece 9): only a check carrying the current attempt's identity counts. The attempt's
identity is one opaque string the collector builds from the dispatch, order and overlay identity of the attempt that
posted the check (J7); this node only compares it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    SHA_PATTERN,
)


class EnumLabCheckConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"


class EnumLabCheckVerdict(StrEnum):
    """What the decision reads off a head's lab-bound check runs."""

    PASS = "pass"
    FAILED = "failed"
    PENDING = "pending"
    MISSING = "missing"
    SUPERSEDED = "superseded"


class ModelLandingLabCheckRun(BaseModel):
    """One lab-bound check run as the GitHub check-run API gives it, with the attempt that posted it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    attempt_id: str = Field(..., min_length=1)
    conclusion: EnumLabCheckConclusion


class ModelLandingLabCheckFacts(BaseModel):
    """The lab-bound check a PR needs before it lands, and every run of it the collector read.

    Optional on the PR's facts: a PR without this fact decides exactly as it did before J8.
    ``dispatched_at`` is when the attempt's job was dispatched; a check not yet posted inside the policy's wait bound
    from then is pending, and past it is missing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_name: str = Field(..., min_length=1)
    current_attempt_id: str = Field(..., min_length=1)
    runs: tuple[ModelLandingLabCheckRun, ...] = ()
    dispatched_at: datetime | None = None


class ModelLabCheckReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumLabCheckVerdict
    detail: str


# ----------------------------------------------------------------------------------------- runner groups


class ModelRepositoryVisibility(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    public: bool


class ModelRunnerGroup(BaseModel):
    """One runner group with the fields GitHub's runner-group API gives; ``lab`` marks a group of lab runners."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    lab: bool
    visibility: Literal["all", "private", "selected"]
    allows_public_repositories: bool
    selected_repositories: tuple[str, ...] = ()


class ModelRunnerGroupSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: datetime
    repositories: tuple[ModelRepositoryVisibility, ...]
    groups: tuple[ModelRunnerGroup, ...]


class ModelRunnerGroupBreach(BaseModel):
    """A public repository a lab runner group lets reach its runners."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group: str
    repository: str
    reason: str


__all__ = [
    "EnumLabCheckConclusion",
    "EnumLabCheckVerdict",
    "ModelLabCheckReading",
    "ModelLandingLabCheckFacts",
    "ModelLandingLabCheckRun",
    "ModelRepositoryVisibility",
    "ModelRunnerGroup",
    "ModelRunnerGroupBreach",
    "ModelRunnerGroupSnapshot",
]
