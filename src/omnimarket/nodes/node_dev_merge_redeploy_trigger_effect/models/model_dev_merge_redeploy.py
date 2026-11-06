# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the lab dev-merge redeploy trigger (OMN-22478, plan section 8 step R2).

A merge to a repository's dev branch arrives as ``ModelRepoMerged``. This node decides
whether the merge reaches the dev lane's runtime, publishes the redeploy-start command for
it, and reads back from the lane that the lane carries the merge. Every deployment fact (the
repositories, their clones, the classifier's location, the lane probe command, the verify
deadline) is the node overlay; nothing in this module names a host, a path or a repository.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_repo_merged import validate_repo_slug


class EnumDevMergeOutcome(StrEnum):
    """What became of one merge. Every merge the node reads ends in exactly one of these."""

    VERIFIED = "verified"
    DECLINED_REPO = "declined_repo"
    DECLINED_BASE = "declined_base"
    DECLINED_NOT_RUNTIME = "declined_not_runtime"
    FAILED_FILES_UNREADABLE = "failed_files_unreadable"
    FAILED_CLASSIFIER = "failed_classifier"
    FAILED_BUILD_CONTEXT_REF = "failed_build_context_ref"
    FAILED_PUBLISH = "failed_publish"
    FAILED_INTERNAL = "failed_internal"
    DEPLOY_NOT_DONE = "deploy_not_done"
    LANE_BEHIND = "lane_behind"
    LANE_UNREADABLE = "lane_unreadable"
    DEADLINE_MISSED = "deadline_missed"


# The outcomes that mean the merge is on the lane, or needed nothing from it.
SUCCESS_OUTCOMES = frozenset(
    {
        EnumDevMergeOutcome.VERIFIED,
        EnumDevMergeOutcome.DECLINED_NOT_RUNTIME,
    }
)


class EnumDevMergeMode(StrEnum):
    """What the node does with a merge that reaches the lane.

    ``publish`` publishes the redeploy-start command itself and waits for the orchestrator's
    terminal event. ``observe`` publishes no command: another trigger (the Actions job, until
    it is cut over) redeploys, and the node only reads the lane until it carries the merge.
    """

    OBSERVE = "observe"
    PUBLISH = "publish"


class ModelDevMergeRepoPolicy(BaseModel):
    """One repository whose dev merges rebuild the dev lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="owner/name on GitHub")
    source_repo: str = Field(
        min_length=1,
        description="The key the lane's build provenance records this repository under.",
    )
    base_branches: tuple[str, ...] = Field(min_length=1)
    clone_dir: str = Field(
        min_length=1, description="This repository's clone on the host that serves."
    )
    build_context_clone_dir: str = Field(
        min_length=1,
        description=(
            "The build-context repository's clone: its tip is the ref the lane rebuilds at."
        ),
    )
    build_context_branch: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_repo(self) -> Self:
        validate_repo_slug(self.repo)
        return self


class ModelDevMergeRedeployPolicy(BaseModel):
    """The node overlay: ``<root>/node_dev_merge_redeploy_trigger_effect/overlay.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: tuple[ModelDevMergeRepoPolicy, ...] = Field(min_length=1)
    classifier_path: str = Field(
        min_length=1,
        description="The deploy-path classifier module the CI gate already uses, loaded by path.",
    )
    lane_probe_argv: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Prints the lane's build-provenance manifest as JSON on stdout. This is the "
            "deployment's own way to reach the lane (for example ssh plus docker exec)."
        ),
    )
    requested_by: str = Field(min_length=1, description="Label carried on the command.")
    mode: EnumDevMergeMode = Field(
        description="observe reads the lane only; publish also publishes the command."
    )
    observe_poll_seconds: float = Field(
        gt=0, description="Seconds between lane reads while observing one merge."
    )
    verify_deadline_seconds: float = Field(
        gt=0,
        description="From publishing the command to a receipt; a missed deadline is a receipt.",
    )

    @model_validator(mode="after")
    def _check_unique(self) -> Self:
        slugs = [r.repo for r in self.repos]
        if len(set(slugs)) != len(slugs):
            raise ValueError("a repository is listed twice in repos")
        return self

    def for_repo(self, repo: str) -> ModelDevMergeRepoPolicy | None:
        return next((r for r in self.repos if r.repo == repo), None)


class ModelDevMergeRedeployReceipt(BaseModel):
    """The one terminal event per merge read: what the node did and what it read back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    repo: str
    pr_number: int = Field(ge=1)
    merge_sha: str
    base: str
    correlation_id: UUID
    mode: EnumDevMergeMode
    outcome: EnumDevMergeOutcome
    command_published: bool
    runtime_paths: int = Field(ge=0)
    final_phase: str = ""
    lane_revision: str = ""
    message: str = Field(default="", max_length=2000)
    observed_at: Annotated[datetime, AwareDatetime]

    @property
    def succeeded(self) -> bool:
        return self.outcome in SUCCESS_OUTCOMES


class ModelDevMergeRedeployTopics(BaseModel):
    """The node's four topics, read from its contract and nowhere else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    merged: str
    start_command: str
    completed: str
    receipt: str


__all__ = [
    "SUCCESS_OUTCOMES",
    "EnumDevMergeMode",
    "EnumDevMergeOutcome",
    "ModelDevMergeRedeployPolicy",
    "ModelDevMergeRedeployReceipt",
    "ModelDevMergeRedeployTopics",
    "ModelDevMergeRepoPolicy",
]
