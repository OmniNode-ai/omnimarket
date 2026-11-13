# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, verdict and events of the code contract score (OMN-23626).

One request names a repository and base commit, the draft as a diff, the ticket contract as
(repository, path, revision, sha256), the check files to place over the worktree and the
try's identity. The overlay values (timeouts, runnable check types, protected paths, the
environment sync command, the clone roots) travel on the request; no key has a default.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.models.enum_code_score import (
    EnumCodeScoreCheckOutcome,
    EnumCodeScoreFailureCode,
    EnumCodeScoreRuntime,
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)

_SHA256 = r"^[0-9a-f]{64}$"
_COMMIT = r"^[0-9a-f]{40}$"


class ModelCodeScoreOverlay(BaseModel):
    """The node's overlay (``code_eval.score.*`` and ``code_eval.protected_paths.<repo>``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_timeout_s: float = Field(gt=0)
    env_sync_timeout_s: float = Field(gt=0)
    runnable_check_types: tuple[str, ...] = Field(min_length=1)
    env_sync_command: str = Field(min_length=1)
    scratch_root: str = Field(min_length=1)
    repo_roots: dict[str, str] = Field(min_length=1)
    protected_paths: dict[str, tuple[str, ...]]
    output_tail_chars: int = Field(ge=0)
    # The bus `onex run-node` reads to reach this node (OMN-24265)
    event_bus: ModelNodeEventBus | None = None


class ModelCodeScoreContractRef(BaseModel):
    """The ticket contract: where it is read from and the sha256 of its text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    path: str = Field(min_length=1)
    revision: str = Field(pattern=_COMMIT)
    sha256: str = Field(pattern=_SHA256)


class ModelCodeScoreCheckFile(BaseModel):
    """One check file placed over the worktree, read at ``revision`` and verified by sha256."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256)
    revision: str = Field(pattern=_COMMIT)


class ModelCodeScoreTry(BaseModel):
    """The identity of the try being scored; the delegation run id is the production join key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_key: str = Field(min_length=1)
    model_arm: str = ""
    context_arm: str = ""
    try_ordinal: int = Field(default=1, ge=1)
    delegation_run_id: UUID | None = None
    timed_out_with_diff: bool = Field(
        default=False,
        description="The draft step hit its wall clock but left this diff; recorded as a flag.",
    )


class ModelCodeScoreRequest(BaseModel):
    """Score one draft against its ticket contract's own bound checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str = Field(min_length=1, description="Key into the overlay's repo_roots.")
    base_commit: str = Field(pattern=_COMMIT)
    draft_diff: str = Field(description="A unified diff; empty is an empty draft.")
    contract: ModelCodeScoreContractRef
    check_files: tuple[ModelCodeScoreCheckFile, ...] = ()
    check_item_ids: tuple[str, ...] = Field(
        default=(),
        description="The contract's dod_evidence item ids whose checks are the score; empty is every item.",
    )
    try_identity: ModelCodeScoreTry
    gate_accepted: bool | None = Field(
        default=None,
        description="The delegation gate's text criteria outcome, recorded and never the score.",
    )
    overlay: ModelCodeScoreOverlay

    @model_validator(mode="after")
    def _repo_has_root(self) -> ModelCodeScoreRequest:
        for name in (self.repo, self.contract.repository):
            if name not in self.overlay.repo_roots:
                raise ValueError(f"overlay names no clone root for repository {name}")
        return self


class ModelCodeScoreCheckResult(BaseModel):
    """One bound check's result in the scratch worktree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    check_type: str
    command: str
    outcome: EnumCodeScoreCheckOutcome
    exit_code: int | None = None
    output_tail: str = ""


class ModelCodeScoreVerdict(BaseModel):
    """The typed verdict: ``checks`` and ``runtime`` levels and, when unscorable, the source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: EnumCodeScoreVerdict
    runtime: EnumCodeScoreRuntime = EnumCodeScoreRuntime.UNREAD_EVAL
    source: EnumCodeScoreSource | None = None
    detail: str = ""
    check_results: tuple[ModelCodeScoreCheckResult, ...] = ()

    @model_validator(mode="after")
    def _source_only_when_unscorable(self) -> ModelCodeScoreVerdict:
        unscorable = self.checks is EnumCodeScoreVerdict.UNSCORABLE
        if unscorable != (self.source is not None):
            raise ValueError(
                "source is set exactly when the checks level is unscorable"
            )
        return self


class ModelCodeScoreTimings(BaseModel):
    """Seconds spent per phase, the environment sync timed apart from the checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    worktree_s: float = Field(default=0.0, ge=0)
    env_sync_s: float = Field(default=0.0, ge=0)
    checks_s: float = Field(default=0.0, ge=0)


class ModelCodeScored(BaseModel):
    """The scored event: one per request, keyed by the request's correlation id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str
    base_commit: str
    draft_diff_sha256: str = Field(pattern=_SHA256)
    try_identity: ModelCodeScoreTry
    gate_accepted: bool | None
    verdict: ModelCodeScoreVerdict
    timings: ModelCodeScoreTimings


class ModelCodeScoreFailed(BaseModel):
    """The failed event: a request refused before any verdict, with a typed code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    code: EnumCodeScoreFailureCode
    detail: str = ""


class ModelCodeScoreResult(BaseModel):
    """What the handler returns: exactly one of the scored or the failed event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scored: ModelCodeScored | None = None
    failed: ModelCodeScoreFailed | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ModelCodeScoreResult:
        if (self.scored is None) == (self.failed is None):
            raise ValueError("exactly one of scored and failed is set")
        return self


__all__ = [
    "ModelCodeScoreCheckFile",
    "ModelCodeScoreCheckResult",
    "ModelCodeScoreContractRef",
    "ModelCodeScoreFailed",
    "ModelCodeScoreOverlay",
    "ModelCodeScoreRequest",
    "ModelCodeScoreResult",
    "ModelCodeScoreTimings",
    "ModelCodeScoreTry",
    "ModelCodeScoreVerdict",
    "ModelCodeScored",
]
