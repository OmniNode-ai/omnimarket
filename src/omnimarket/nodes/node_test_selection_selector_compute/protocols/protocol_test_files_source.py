# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the selector reads the list of test files of a checkout from."""

from __future__ import annotations

from typing import Protocol


class ProtocolTestFilesSource(Protocol):
    def list_test_files(self, repo_root: str) -> tuple[str, ...] | None:
        """Repository-relative paths of every test file, sorted, or None when they cannot be listed."""
        ...

    def file_exists(self, repo_root: str, path: str) -> bool:
        """Whether the repository-relative path names a file of the checkout."""
        ...
