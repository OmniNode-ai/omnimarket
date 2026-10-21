# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""How urgent a local environment alert is."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumLocalAlertSeverity(StrEnum):
    """Critical alerts show as a banner; warnings show on the panel only."""

    CRITICAL = "critical"
    WARNING = "warning"


__all__ = ["EnumLocalAlertSeverity"]
