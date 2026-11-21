# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refusals a panel row records as its last refusal."""

from __future__ import annotations

from enum import StrEnum


class EnumPanelRefusal(StrEnum):
    UNDECODABLE = "undecodable"
    FUTURE_DATED = "future_dated"


__all__ = ["EnumPanelRefusal"]
