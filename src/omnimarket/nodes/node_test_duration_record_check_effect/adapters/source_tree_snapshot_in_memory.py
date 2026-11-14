# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An in-memory tree snapshot source for tests: each commit holds the files and record it was given."""

from __future__ import annotations


class SourceTreeSnapshotInMemory:
    """Returns the fixed ``(files, record)`` pair registered for each commit sha."""

    def __init__(
        self, snapshots: dict[str, tuple[tuple[str, ...], dict[str, float]]]
    ) -> None:
        self._snapshots = snapshots

    def test_tree_files(self, repo_root: str, sha: str) -> tuple[str, ...] | None:
        """Return the files registered for ``sha``, or ``None`` for an unknown commit."""
        snapshot = self._snapshots.get(sha)
        return None if snapshot is None else snapshot[0]

    def record_entries(self, repo_root: str, sha: str) -> dict[str, float] | None:
        """Return the record registered for ``sha``, or ``None`` for an unknown commit."""
        snapshot = self._snapshots.get(sha)
        return None if snapshot is None else dict(snapshot[1])
