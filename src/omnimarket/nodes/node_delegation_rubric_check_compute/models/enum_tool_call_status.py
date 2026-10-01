# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed ToolCallStatus contract."""

from enum import StrEnum


class EnumToolCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
