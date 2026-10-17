# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The free-text slots of a lane brief, the only parts the model writes."""

from __future__ import annotations

from enum import StrEnum


class EnumBriefDraftSlot(StrEnum):
    """Closed set. Every other part of a brief is a shell part filled by code."""

    TITLE = "title"
    PREMISES = "premises"
    TASK = "task"
