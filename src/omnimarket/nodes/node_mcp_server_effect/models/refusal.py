# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed refusals of the MCP server node."""

from __future__ import annotations

from enum import StrEnum


class EnumMcpServerRefusal(StrEnum):
    REGISTRY_UNREADABLE = "registry_unreadable"
    DUPLICATE_TOOL_NAME = "duplicate_tool_name"


class McpServerRefusalError(Exception):
    """A named refusal; never an empty list in place of an answer."""

    def __init__(self, refusal: EnumMcpServerRefusal, message: str) -> None:
        super().__init__(f"{refusal.value}: {message}")
        self.refusal = refusal
