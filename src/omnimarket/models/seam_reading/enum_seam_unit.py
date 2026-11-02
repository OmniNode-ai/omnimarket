# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a seam measure counts."""

from __future__ import annotations

from enum import StrEnum


class EnumSeamUnit(StrEnum):
    SOURCES = "sources"
    ROWS = "rows"
    LINES = "lines"
    SECTIONS = "sections"
    DECLARATIONS = "declarations"
    COMPOSE_INPUTS = "compose-inputs"
    VERDICT_ROWS = "verdict-rows"
    COMMANDS = "commands"
    TOKENS = "tokens"
    ROLES = "roles"
    THEME = "theme"
    ARTIFACTS = "artifacts"
    LINE = "line"
    REQUESTS = "requests"
    READINGS = "readings"
    FINDINGS = "findings"
