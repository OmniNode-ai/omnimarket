# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the selector reads recorded per-file test seconds from (OMN-23296)."""

from __future__ import annotations

from typing import Protocol


class ProtocolDurationsSource(Protocol):
    def read(self, repo_root: str, durations_path: str) -> dict[str, float] | None:
        """Recorded seconds by test file, or None when they cannot be read."""
        ...
