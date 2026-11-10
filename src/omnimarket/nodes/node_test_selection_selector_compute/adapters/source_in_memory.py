# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory durations source: the default backend, and the tests'."""

from __future__ import annotations


class SourceInMemory:
    """Answers the durations it was built with; ``None`` stands for an unreadable file."""

    def __init__(self, durations: dict[str, float] | None = None) -> None:
        self._durations = durations

    def read(self, repo_root: str, durations_path: str) -> dict[str, float] | None:
        return self._durations
