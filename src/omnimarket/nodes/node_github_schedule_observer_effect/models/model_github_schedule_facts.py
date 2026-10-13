# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parts of GitHub's responses the observer reads (OMN-20803).

GitHub's bodies carry many more fields; only these are read, and a body that
lacks one is unreadable, never defaulted.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelGithubWorkflowRun(BaseModel):
    """One entry of ``GET /repos/{o}/{r}/actions/runs``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int
    path: str
    status: str
    conclusion: str | None = None
    created_at: AwareDatetime
    run_started_at: AwareDatetime | None = None
    updated_at: AwareDatetime
    run_attempt: int = Field(default=1, ge=1)

    @property
    def workflow_file(self) -> str:
        """The file name under .github/workflows, without any ``@ref`` suffix."""
        return self.path.split("@", 1)[0].rsplit("/", 1)[-1]


class ModelGithubWorkflow(BaseModel):
    """One entry of ``GET /repos/{o}/{r}/actions/workflows``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    path: str
    state: str

    @property
    def workflow_file(self) -> str:
        return self.path.split("@", 1)[0].rsplit("/", 1)[-1]


class ModelGithubPullBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    ref: str


class ModelGithubClosedPull(BaseModel):
    """One entry of ``GET /repos/{o}/{r}/pulls?state=closed``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    number: int = Field(ge=1)
    updated_at: AwareDatetime
    merged_at: AwareDatetime | None = None
    merge_commit_sha: str | None = None
    base: ModelGithubPullBase


class ModelGithubPullFile(BaseModel):
    """One entry of ``GET /repos/{o}/{r}/pulls/{n}/files``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    filename: str
