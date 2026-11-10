# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the files a change adds from git, staged or against a base ref."""

from __future__ import annotations

import subprocess


class SourceAddedFilesGit:
    """Lists added files with ``git diff --name-only --diff-filter=AR``."""

    def added_files(
        self, repo_root: str, base_ref: str | None
    ) -> tuple[str, ...] | None:
        """Return the added files, or ``None`` when git could not be read."""
        if base_ref is None:
            argv = [
                "git",
                "-C",
                repo_root,
                "diff",
                "--cached",
                "--name-only",
                "--diff-filter=AR",
            ]
        else:
            argv = [
                "git",
                "-C",
                repo_root,
                "diff",
                "--name-only",
                "--diff-filter=AR",
                f"{base_ref}...HEAD",
            ]
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            return None
        return tuple(line for line in completed.stdout.splitlines() if line.strip())
