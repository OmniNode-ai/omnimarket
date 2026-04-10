# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol interface for git/worktree operations used by HandlerTicketWork."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ModelWorktreeInfo(BaseModel):
    """Info about an existing or newly-created worktree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., description="Absolute filesystem path.")
    branch: str = Field(..., description="Branch name checked out in the worktree.")
    created: bool = Field(default=True, description="True if newly created.")


class ModelRunResult(BaseModel):
    """Result of running a shell command in a worktree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    exit_code: int
    stdout: str = Field(default="")
    stderr: str = Field(default="")

    @property
    def success(self) -> bool:
        return self.exit_code == 0


@runtime_checkable
class ProtocolGitClient(Protocol):
    """Protocol for git/worktree/CI operations.

    Implementations inject the real subprocess-backed client; tests inject stubs.
    """

    def create_or_checkout_worktree(
        self,
        repo_path: str,
        ticket_id: str,
        branch_name: str,
    ) -> ModelWorktreeInfo:
        """Create a git worktree for the given ticket and branch.

        If the branch already exists, checks it out without creating.
        Returns ModelWorktreeInfo describing the worktree.
        """
        ...

    def install_pre_commit(self, worktree_path: str) -> bool:
        """Install pre-commit hooks in the worktree. Returns True on success."""
        ...

    def run_pre_commit(self, worktree_path: str) -> ModelRunResult:
        """Run pre-commit against all files. Returns result."""
        ...

    def run_tests(self, worktree_path: str) -> ModelRunResult:
        """Run the test suite. Returns result."""
        ...

    def commit_changes(
        self,
        worktree_path: str,
        message: str,
    ) -> ModelRunResult:
        """Stage all changes and commit. Returns result with commit SHA in stdout."""
        ...

    def push_branch(self, worktree_path: str, branch: str) -> ModelRunResult:
        """Push branch to origin. Returns result."""
        ...

    def create_pr(
        self,
        worktree_path: str,
        title: str,
        body: str,
    ) -> ModelRunResult:
        """Create a GitHub PR via gh cli. Returns result with PR URL in stdout."""
        ...


__all__: list[str] = [
    "ModelRunResult",
    "ModelWorktreeInfo",
    "ProtocolGitClient",
]
