# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The commands of the fill's entry."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorFillCommand(StrEnum):
    """``tick`` (a whole pass), ``send_key`` (one key), ``inspect`` and ``lines`` (read-only)."""

    TICK = "tick"
    SEND_KEY = "send_key"
    INSPECT = "inspect"
    LINES = "lines"


__all__: list[str] = ["EnumFloorFillCommand"]
