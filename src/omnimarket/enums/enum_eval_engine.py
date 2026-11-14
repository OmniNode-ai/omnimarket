# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which engine answered a scored try (OMN-24170)."""

from __future__ import annotations

from enum import StrEnum


class EnumEvalEngine(StrEnum):
    """The engine that produced the answer being scored."""

    LOCAL = "local"
    GLM = "glm"
    CODEX = "codex"
    CLAUDE = "claude"


__all__ = ["EnumEvalEngine"]
