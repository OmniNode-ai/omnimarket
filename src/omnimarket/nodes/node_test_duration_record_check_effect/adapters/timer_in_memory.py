# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An in-memory timer for tests: it returns the seconds it was given per file."""

from __future__ import annotations


class TimerInMemory:
    """Returns the seconds it was constructed with for a file, or ``None`` when absent."""

    def __init__(self, seconds: dict[str, float]) -> None:
        self._seconds = seconds

    def measure(self, repo_root: str, test_file: str) -> float | None:
        """Return the seconds for ``test_file``, or ``None`` when it has no entry."""
        return self._seconds.get(test_file)
