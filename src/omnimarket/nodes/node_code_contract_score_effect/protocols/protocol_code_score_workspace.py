# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of node_code_contract_score_effect: the git workspace, the check runner and the event bus (OMN-23626)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class WorkspaceError(Exception):
    """A scratch worktree step failed for a reason that is not the draft's own."""


@dataclass(frozen=True)
class RunOutcome:
    """One command run in a worktree."""

    exit_code: int | None
    timed_out: bool
    duration_s: float
    output_tail: str


@runtime_checkable
class ProtocolCodeScoreWorkspace(Protocol):
    """Scratch worktree operations at a base commit."""

    def read_blob(self, repo_root: str, revision: str, path: str) -> bytes | None:
        """The file at ``revision``, or None when it does not exist there."""
        ...

    def create(self, repo_root: str, base_commit: str, scratch_root: str) -> Path:
        """A new detached worktree at ``base_commit`` under ``scratch_root``."""
        ...

    def apply_diff(self, worktree: Path, diff: str) -> str | None:
        """Apply the diff; None when it applied, else the reason it did not."""
        ...

    def place_file(self, worktree: Path, path: str, data: bytes) -> None:
        """Write ``data`` at ``path`` inside the worktree."""
        ...

    def remove(self, repo_root: str, worktree: Path) -> None:
        """Remove the worktree."""
        ...


@runtime_checkable
class ProtocolCodeScoreRunner(Protocol):
    """Run one shell command in a worktree under a wall-clock limit."""

    def run(
        self, command: str, cwd: Path, timeout_s: float, tail_chars: int
    ) -> RunOutcome:
        """Run ``command``; a limit hit is ``timed_out``, never an exit code."""
        ...


@runtime_checkable
class ProtocolCodeScoreEventPublisher(Protocol):
    """Publish one serialized event on a topic (the event bus client's shape)."""

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        """Deliver ``value`` on ``topic``."""
        ...


__all__ = [
    "ProtocolCodeScoreEventPublisher",
    "ProtocolCodeScoreRunner",
    "ProtocolCodeScoreWorkspace",
    "RunOutcome",
    "WorkspaceError",
]
