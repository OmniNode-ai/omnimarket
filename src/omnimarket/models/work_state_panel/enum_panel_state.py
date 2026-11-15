# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The panel's read state, and the row state the persisted row reports."""

from __future__ import annotations

from enum import StrEnum


class EnumPanelState(StrEnum):
    READ = "read"
    STALE = "stale"
    UNREAD = "unread"


class EnumPanelRowState(StrEnum):
    EMPTY = "EMPTY"
    HELD = "HELD"


__all__ = ["EnumPanelRowState", "EnumPanelState"]
