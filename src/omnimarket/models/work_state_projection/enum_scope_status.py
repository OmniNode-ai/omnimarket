# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether the goal's scope declaration takes an item in."""

from __future__ import annotations

from enum import StrEnum


class EnumScopeStatus(StrEnum):
    """Whether the goal's scope declaration takes an item in."""

    IN_SCOPE = "in_scope"
    PAUSED = "paused"
