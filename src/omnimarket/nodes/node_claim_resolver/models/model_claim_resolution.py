# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed claim-resolution contract for agent result verification."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumAgentClaimKind(StrEnum):
    """Agent-side-effect claim kinds verified by node_claim_resolver."""

    PR_MERGED = "pr_merged"
    PR_OPENED = "pr_opened"
    COMMIT_SHA = "commit_sha"
    CI_PASSING = "ci_passing"
    FILE_COMMITTED = "file_committed"
    BLOCKER_ON_X = "blocker_on_X"
    THREAD_RESOLVED = "thread_resolved"
    LINEAR_STATE = "linear_state"


class EnumClaimResolutionStatus(StrEnum):
    """Per-claim verification status."""

    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"


class ModelAgentClaim(BaseModel):
    """A single agent turn claim normalized by the hook extractor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumAgentClaimKind = Field(..., description="Claim taxonomy kind.")
    ref: str = Field(..., min_length=1, description="Canonical claim reference.")
    expected: str | None = Field(
        default=None,
        description="Expected value for stateful claims, when applicable.",
    )
    evidence: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Quoted command output or cited evidence snippets.",
    )


class ModelClaimResolutionRequest(BaseModel):
    """Batch request for resolver-backed claim verification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: tuple[ModelAgentClaim, ...] = Field(default_factory=tuple)
    repo_hint: str | None = Field(
        default=None,
        description="Repository slug used to qualify bare PR refs.",
    )
    repo_root: str | None = Field(
        default=None,
        description="Git repository path for commit/file checks.",
    )


class ModelClaimResolutionResult(BaseModel):
    """Single claim verification result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: ModelAgentClaim
    status: EnumClaimResolutionStatus
    reason: str
    expected: str | None = None
    actual: str | None = None
    evidence: tuple[str, ...] = Field(default_factory=tuple)


class ModelClaimResolutionResponse(BaseModel):
    """Batch response returned by node_claim_resolver."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    results: tuple[ModelClaimResolutionResult, ...] = Field(default_factory=tuple)
    mismatches: tuple[ModelClaimResolutionResult, ...] = Field(default_factory=tuple)


__all__ = [
    "EnumAgentClaimKind",
    "EnumClaimResolutionStatus",
    "ModelAgentClaim",
    "ModelClaimResolutionRequest",
    "ModelClaimResolutionResponse",
    "ModelClaimResolutionResult",
]
