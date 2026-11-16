# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam that lists the files a change adds, so the handler can be tested without git."""

from __future__ import annotations

from typing import Protocol


class ProtocolAddedFilesSource(Protocol):
    """Lists the files a change adds, or ``None`` when git could not be read."""

    def added_files(
        self, repo_root: str, base_ref: str | None
    ) -> tuple[str, ...] | None:
        """Return the added files, or ``None`` when git could not be read."""
