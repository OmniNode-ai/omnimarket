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
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.dev_merge_redeploy_overlay import EnumDevMergeMode


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
    "EnumDevMergeOutcome",
    "ModelDevMergeRedeployReceipt",
    "ModelDevMergeRedeployTopics",
]
