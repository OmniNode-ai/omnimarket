# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which way a local alert moved."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumLocalAlertTransition(StrEnum):
    """An alert is raised by a verdict that opens it and cleared by a later one that does not."""

    RAISED = "raised"
    CLEARED = "cleared"


__all__ = ["EnumLocalAlertTransition"]
