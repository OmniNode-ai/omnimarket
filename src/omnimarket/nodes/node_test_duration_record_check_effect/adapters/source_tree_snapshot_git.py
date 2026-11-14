# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a commit's test files and durations record from git without checking the commit out."""

from __future__ import annotations

import json
import subprocess

from ..models.model_test_duration_record_check import ModelTestDurationRecordPolicy


class SourceTreeSnapshotGit:
    """Lists a commit's tests with ``git ls-tree`` and reads its record with ``git show``."""

    def __init__(self, policy: ModelTestDurationRecordPolicy) -> None:
        self._policy = policy

    def test_tree_files(self, repo_root: str, sha: str) -> tuple[str, ...] | None:
        """Return every file under the tests directory at ``sha``, or ``None`` when git failed."""
        return self._ls_tree(repo_root, sha, self._policy.tests_dir)

    def record_entries(self, repo_root: str, sha: str) -> dict[str, float] | None:
        """Return the shared list merged with its per-file entries at ``sha``."""
        merged: dict[str, float] = {}
        sidecars = self._ls_tree(repo_root, sha, self._policy.record_dir)
        if sidecars is None:
            return None
        for name in sidecars:
            entry = self._read_json(repo_root, sha, name)
            if entry is None:
                return None
            merged.update(entry)
        shared = self._read_json(repo_root, sha, self._policy.record_path)
        if shared is None:
            return None
        merged.update(shared)
        return merged

    def _ls_tree(
        self, repo_root: str, sha: str, directory: str
    ) -> tuple[str, ...] | None:
        """List the files under ``directory`` at ``sha``; a missing directory is an empty list."""
        completed = subprocess.run(
            [
                "git",
                "-C",
                repo_root,
                "ls-tree",
                "-r",
                "--name-only",
                sha,
                "--",
                directory,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return None
        return tuple(line for line in completed.stdout.splitlines() if line.strip())

    def _read_json(
        self, repo_root: str, sha: str, path: str
    ) -> dict[str, float] | None:
        """Read one JSON object file at ``sha``, or ``None`` when it is absent or not an object."""
        completed = subprocess.run(
            ["git", "-C", repo_root, "show", f"{sha}:{path}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return None
        try:
            data = json.loads(completed.stdout)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
