# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a work item is."""

from __future__ import annotations

from enum import StrEnum


class EnumWorkItemKind(StrEnum):
    """What a work item is."""

    TICKET = "ticket"
    PLAN_TASK = "plan_task"
    PULL_REQUEST = "pull_request"
    RED_CHECK = "red_check"
    LANE = "lane"
