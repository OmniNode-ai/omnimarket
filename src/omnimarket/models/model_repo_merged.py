# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Payload of ``onex.evt.omnimarket.repo-merged.v1``, shared by its consumers (OMN-19998).

``clone-refresh publish`` puts one of these on the bus for every merge the landing
controller makes and every merge the PR watcher observes and did not make (OMN-20496).
Two nodes read it: ``node_canonical_clone_refresh_effect`` syncs each host's canonical
clones, and ``node_pr_merged_projection`` folds it into ``pr_merged_events`` so the
merged-PR feed does not depend on the Actions publisher. The model lives here, not in
either node, so neither imports the other's private package.
"""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

# Every repository the PR watcher and the landing controller watch is in this organization,
# so the owner check is the contract, not a limit on a multi-owner feed. It is carried over
# unchanged from the model's original home in ``node_canonical_clone_refresh_effect``; both
# consumers keep the restriction through ``validate_repo_slug``.
ALLOWED_OWNER = "OmniNode-ai"
_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def validate_repo_slug(value: str) -> str:
    if not _REPO_RE.match(value):
        raise ValueError(f"repo must be owner/name: {value!r}")
    if value.split("/", 1)[0] != ALLOWED_OWNER:
        raise ValueError(f"repo owner must be {ALLOWED_OWNER}: {value!r}")
    return value


def validate_git_sha(value: str) -> str:
    if not _SHA_RE.match(value):
        raise ValueError(f"sha must be 40 lowercase hex: {value!r}")
    return value


class ModelRepoMerged(BaseModel):
    """A pull request merged into a repository branch: the event every host's clone follows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="owner/name on GitHub")
    base: str = Field(min_length=1, max_length=255)
    merge_sha: str
    pr_number: int = Field(ge=1)
    merged_at: Annotated[datetime, AwareDatetime]
    source: str = Field(min_length=1, max_length=64)
    state: Literal["merged", "open", "closed"] = "merged"

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        return validate_repo_slug(value)

    @field_validator("merge_sha")
    @classmethod
    def _check_sha(cls, value: str) -> str:
        return validate_git_sha(value)


__all__ = [
    "ALLOWED_OWNER",
    "ModelRepoMerged",
    "validate_git_sha",
    "validate_repo_slug",
]
