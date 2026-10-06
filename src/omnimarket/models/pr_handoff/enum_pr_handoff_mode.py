# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How the requesting lane closes: its own TERMINAL, or a session lane's STATUS."""

from __future__ import annotations

from enum import StrEnum


class EnumPrHandoffMode(StrEnum):
    """How the requesting lane closes: its own TERMINAL, or a session lane's STATUS."""

    LANE = "lane"
    """The lane writes TERMINAL outcome=handed-off (or nothing with msg_only)."""

    SESSION = "session"
    """The orchestrator's session lane writes STATUS handed-off=<repo#n>; its CLAIM stays open."""


__all__: list[str] = ["EnumPrHandoffMode"]
