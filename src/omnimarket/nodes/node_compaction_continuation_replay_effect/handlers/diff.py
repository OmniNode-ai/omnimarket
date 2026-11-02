# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Diff collection for the scratch worktree: `git diff HEAD`, so staged and unstaged changes
both appear (a bare `git diff` omits staged ones)."""

from __future__ import annotations

import os
import subprocess

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)


def collect_diff(worktree: str) -> str:
    done = subprocess.run(
        ["git", "-C", worktree, "diff", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout
