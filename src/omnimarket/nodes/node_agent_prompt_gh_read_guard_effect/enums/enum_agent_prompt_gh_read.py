# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verdict and session mode of the agent-prompt gh-read guard (OMN-23307)."""

from enum import StrEnum


class EnumAgentPromptGhReadVerdict(StrEnum):
    """ALLOW lets the dispatch run; REFUSE blocks it with the mirror verbs named."""

    ALLOW = "ALLOW"
    REFUSE = "REFUSE"


class EnumGuardSessionMode(StrEnum):
    """omniclaude_mode of the session: lite (a non-OmniNode install) refuses nothing."""

    FULL = "full"
    LITE = "lite"
