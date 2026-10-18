# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registration's outcome for one Codex hooks file (OMN-18759)."""

from enum import StrEnum


class EnumCodexHookRegistrationStatus(StrEnum):
    """Whether the user hooks file needed or received missing commands."""

    UNCHANGED = "unchanged"
    WOULD_ADD = "would_add"
    ADDED = "added"
    FAILED = "failed"
