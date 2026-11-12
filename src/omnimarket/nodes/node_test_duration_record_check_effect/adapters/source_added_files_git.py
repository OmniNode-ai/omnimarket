# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the files a change adds from git, staged or against a base ref."""

from __future__ import annotations

import subprocess


class SourceAddedFilesGit:
    """Lists added files with ``git diff --name-only --diff-filter=AR``.

    During a merge in progress (``MERGE_HEAD`` exists), the staged mode lists
    only the files added against both parents, so a hook on a merge commit
    refuses just the additions this change staged, not the ones the
    merged-in branch added.
    """

    def added_files(
        self, repo_root: str, base_ref: str | None
    ) -> tuple[str, ...] | None:
        """Return the added files, or ``None`` when git could not be read."""
        if base_ref is None:
            return self._staged_added_files(repo_root)
        argv = [
            "git",
            "-C",
            repo_root,
            "diff",
            "--name-only",
            "--diff-filter=AR",
            f"{base_ref}...HEAD",
        ]
        return self._diff_additions(argv)

    def _staged_added_files(self, repo_root: str) -> tuple[str, ...] | None:
        """Return the staged additions, or the both-parent additions in a merge."""
        merge_head = subprocess.run(
            [
                "git",
                "-C",
                repo_root,
                "rev-parse",
                "-q",
                "--verify",
                "MERGE_HEAD",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if merge_head.returncode != 0:
            return self._diff_additions(
                [
                    "git",
                    "-C",
                    repo_root,
                    "diff",
                    "--cached",
                    "--name-only",
                    "--diff-filter=AR",
                ]
            )
        against_head = self._diff_additions(
            [
                "git",
                "-C",
                repo_root,
                "diff",
                "--cached",
                "--name-only",
                "--diff-filter=AR",
                "HEAD",
            ]
        )
        against_merge_head = self._diff_additions(
            [
                "git",
                "-C",
                repo_root,
                "diff",
                "--cached",
                "--name-only",
                "--diff-filter=AR",
                "MERGE_HEAD",
            ]
        )
        if against_head is None or against_merge_head is None:
            return None
        merge_added = set(against_merge_head)
        return tuple(name for name in against_head if name in merge_added)

    def _diff_additions(self, argv: list[str]) -> tuple[str, ...] | None:
        """Run one ``git diff --name-only --diff-filter=AR`` and read its lines."""
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            return None
        return tuple(line for line in completed.stdout.splitlines() if line.strip())
