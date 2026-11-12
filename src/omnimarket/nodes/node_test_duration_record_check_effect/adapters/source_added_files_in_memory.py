# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An in-memory added-files source for tests: it always returns the files it was given."""

from __future__ import annotations


class SourceAddedFilesInMemory:
    """Returns the fixed tuple of files it was constructed with."""

    def __init__(self, files: tuple[str, ...]) -> None:
        self._files = files

    def added_files(
        self, repo_root: str, base_ref: str | None
    ) -> tuple[str, ...] | None:
        """Return the fixed files, ignoring ``repo_root`` and ``base_ref``."""
        return self._files
