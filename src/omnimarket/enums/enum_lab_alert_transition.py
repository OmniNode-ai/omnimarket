# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from enum import StrEnum, unique


@unique
class EnumLabAlertTransition(StrEnum):
    """A lab alert is raised by a source that sees its condition and cleared by that source seeing it end."""

    RAISED = "raised"
    CLEARED = "cleared"


__all__ = ["EnumLabAlertTransition"]
