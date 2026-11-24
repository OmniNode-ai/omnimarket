# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed set of agent platforms that send and read agent messages."""

from __future__ import annotations

from enum import StrEnum


class EnumAgentMessagePlatform(StrEnum):
    """Which agent platform a message was sent from; a name outside the set is refused, not guessed."""

    CLAUDE = "claude"
    CODEX = "codex"
    GLM = "glm"
    HERMES = "hermes"
    GEMINI = "gemini"
    OTHER = "other"
