# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Severity of a runtime error (OMN-18770)."""

from __future__ import annotations

from enum import StrEnum


class EnumRuntimeErrorSeverity(StrEnum):
    """How bad the emitting process said it was."""

    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"


__all__ = ["EnumRuntimeErrorSeverity"]
