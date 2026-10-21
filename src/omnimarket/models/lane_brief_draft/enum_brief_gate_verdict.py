# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the brief checker's result means for the draft."""

from __future__ import annotations

from enum import StrEnum


class EnumBriefGateVerdict(StrEnum):
    """Closed set. Only ACCEPTED is dispatchable; the rest name the next move."""

    ACCEPTED = "accepted"
    REGENERATE_SHELL = "regenerate_shell"
    REDRAFT_LOCAL = "redraft_local"
    WRITE_WITH_CLAUDE = "write_with_claude"
    UNREADABLE = "unreadable"
