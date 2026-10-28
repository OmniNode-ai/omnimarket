# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The engines a brief may require."""

from __future__ import annotations

from enum import StrEnum


class EnumLaneEngine(StrEnum):
    """Closed set. A brief's engine line is one of these words and nothing else."""

    CLAUDE = "claude"
    CLAUDE_SONNET = "claude-sonnet"
    CLAUDE_OPUS = "claude-opus"
    CODEX = "codex"
    GLM = "glm"
