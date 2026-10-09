# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, policy and result of one shadow-review tick (OMN-20422).

The policy defaults are the pre-registered scope of the shadow reviewer
experiment: the public stratum gets both arms, the private stratum gets the
Codex arm only, knowledge-base-internal is never read, and the sample is capped
at the first 60 qualifying public PRs created at or after the window start.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class EnumShadowStratum(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    EXCLUDED = "excluded"


class EnumShadowArm(StrEnum):
    CODEX = "A1"
    GLM = "A2"


class EnumShadowArmStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"


class EnumShadowDecision(StrEnum):
    REVIEW = "review"
    SKIP = "skip"


class ModelShadowReviewCandidate(BaseModel):
    """One PR as the PR watcher observed it. Bodies are never carried."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: str = Field(min_length=1)
    number: int = Field(gt=0)
    created_at: str = Field(min_length=1)
    head_sha: str = Field(min_length=7)
    head_ref: str = Field(min_length=1)
    base: str = Field(min_length=1)
    title: str = ""
    author: str = ""
    author_is_bot: bool = False
    draft: bool = False
    evidence_companion: str | None = None
    watcher_class: str = ""

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


class ModelShadowReviewPolicy(BaseModel):
    """Pre-registered scope (PREREGISTRATION.T0.md sections 3, 4 and 7).

    knowledge-base-internal is excluded outright: the consent row that governs
    the run lets Codex and GLM read every repository except that one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    public_repos: tuple[str, ...] = (
        "omnibase_infra",
        "omnibase_spi",
        "omniclaude",
        "omnidash",
        "omnigemini",
        "omniintelligence",
        "omnimarket",
        "omnimemory",
        "onex_change_control",
    )
    private_repos: tuple[str, ...] = (
        "omninode_infra",
        "omnibase_internal",
        "omniclaude-internal",
    )
    excluded_repos: tuple[str, ...] = ("knowledge-base-internal",)
    companion_authors: tuple[str, ...] = ("onexbot-occ-writer[bot]",)
    window_start: str | None = None
    public_cap: int = Field(default=60, ge=1)
    max_reviews_per_tick: int = Field(default=2, ge=1)
    codex_timeout_s: float = Field(default=600.0, gt=0)
    glm_call_budget_s: float = Field(default=600.0, gt=0)


class ModelShadowReviewRequest(BaseModel):
    """One tick: the watcher's PRs, the scope and where the store lives.

    ``only`` names PR keys (``repo#n``) to review regardless of the window,
    for a one-PR proof into a store that is not the sample store. Scope,
    qualification and the secret scan still apply to them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: tuple[ModelShadowReviewCandidate, ...]
    policy: ModelShadowReviewPolicy = Field(default_factory=ModelShadowReviewPolicy)
    store_root: Path
    only: tuple[str, ...] = ()
    dry_run: bool = False


class ModelShadowSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    stratum: EnumShadowStratum
    decision: EnumShadowDecision
    reason: str


class ModelShadowArmResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: EnumShadowArm
    status: EnumShadowArmStatus
    wall_s: float
    finding_count: int = 0
    findings: JsonValue = Field(default_factory=list)
    error: str | None = None
    detail: dict[str, str] = Field(default_factory=dict)


class ModelShadowReviewRecord(BaseModel):
    """What the store keeps for one reviewed PR, one file per PR."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    repo: str
    number: int
    stratum: EnumShadowStratum
    created_at: str
    head_sha: str
    base: str
    first_observed_at: str
    reviewed_at: str
    diff_sha256: str
    diff_bytes: int
    arms: tuple[ModelShadowArmResult, ...]
    posted: bool = False


class ModelShadowReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_root: Path
    window_start: str | None
    selections: tuple[ModelShadowSelection, ...]
    records: tuple[ModelShadowReviewRecord, ...]
    dropped: tuple[ModelShadowSelection, ...]
    public_reviewed_total: int
    dry_run: bool
