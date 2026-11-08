# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a work item stands."""

from __future__ import annotations

from enum import StrEnum


class EnumWorkItemState(StrEnum):
    """Where a work item stands."""

    AVAILABLE = "available"
    BLOCKED = "blocked"
    STOPPED = "stopped"
    IN_PROGRESS = "in_progress"
    DONE = "done"
