# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a red CI Summary left by cancelled or skipped siblings needs first."""

from __future__ import annotations

from enum import StrEnum


class EnumStaleSummaryKind(StrEnum):
    RERUN = "rerun"
    REFRESH = "refresh"
    REFRESH_FIRST = "refresh_first"


__all__: list[str] = ["EnumStaleSummaryKind"]
