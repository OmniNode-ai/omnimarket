# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment overlay of the dev-merge redeploy node (OMN-22478, plan section 8 step R2).

It lives apart from the node package so that the private overlays repository's check can load an
overlay through this model with only pydantic and pyyaml installed: importing the node package
pulls in the bus and the handlers. Every deployment fact (the repositories, their clones, the
classifier's location, the lane probe command, the mode, the verify deadline) is in the overlay;
nothing in this module names a host, a path or a repository.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_repo_merged import validate_repo_slug


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
    provenance_section: Literal["siblings", "build_context"] = Field(
        description=(
            "Where the lane's build provenance records this repository: a vendored sibling "
            "(per_repo_vcs_provenance.siblings) or the build context itself "
            "(per_repo_vcs_provenance.build_context)."
        )
    )

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


__all__ = [
    "EnumDevMergeMode",
    "ModelDevMergeRedeployPolicy",
    "ModelDevMergeRepoPolicy",
]
