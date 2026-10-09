# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Supported internal coding-agent harnesses."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumHarness(StrEnum):
    """Supported internal coding-agent harnesses."""

    CODEX = "codex"
    CLAUDE_GLM = "claude-glm"
    CLAUDE = "claude"


__all__: list[str] = ["EnumHarness"]
