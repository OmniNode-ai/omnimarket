# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why an arm comparison was refused instead of measured."""

from __future__ import annotations

from enum import StrEnum


class EnumArmCompareFailure(StrEnum):
    """Closed set. A failed comparison carries no verdict."""

    DUPLICATE_TASK = "duplicate_task"
    SAME_ARM = "same_arm"
    NO_TASKS = "no_tasks"
    OVERLAY_UNAVAILABLE = "overlay_unavailable"
