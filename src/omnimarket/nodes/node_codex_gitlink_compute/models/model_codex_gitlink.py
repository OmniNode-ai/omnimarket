# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and rendered shell lines for a Codex lane's worktree ``.git`` link."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCodexGitlinkRequest:
    lock_reason: str
    gitlink: str
    mounts: tuple[str, ...]


@dataclass(frozen=True)
class ModelCodexGitlinkResult:
    aside_line: str
    put_back_line: str
    restore_line: str
