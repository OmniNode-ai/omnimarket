# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a run is superseded (OMN-17427)."""

from __future__ import annotations

from enum import StrEnum


class EnumSupersededReason(StrEnum):
    """The two ways a CI run is left behind."""

    PR_HEAD_MOVED = "pr_head_moved"
    MERGE_GROUP_LEFT_QUEUE = "merge_group_left_queue"
