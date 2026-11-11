# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam that times one test file, so the handler can be tested without pytest."""

from __future__ import annotations

from typing import Protocol


class ProtocolFileTimer(Protocol):
    """Times one test file and reports its seconds, or ``None`` when it could not be timed."""

    def measure(self, repo_root: str, test_file: str) -> float | None:
        """Return the seconds the file's tests took, or ``None`` when they fail or did not run."""
