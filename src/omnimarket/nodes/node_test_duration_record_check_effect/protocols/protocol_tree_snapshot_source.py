# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam that reads a commit's test files and durations record, so the freshness check needs no git in tests."""

from __future__ import annotations

from typing import Protocol


class ProtocolTreeSnapshotSource(Protocol):
    """Reads what one commit holds: its files under the tests directory and its durations record."""

    def test_tree_files(self, repo_root: str, sha: str) -> tuple[str, ...] | None:
        """Return every file under the tests directory at ``sha``, or ``None`` when it cannot be read."""

    def record_entries(self, repo_root: str, sha: str) -> dict[str, float] | None:
        """Return the shared list merged with its per-file entries at ``sha``, or ``None`` when unreadable."""
