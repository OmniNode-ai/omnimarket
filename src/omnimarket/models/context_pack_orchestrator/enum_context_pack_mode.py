# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether the sources read live state or a pinned replay case."""

from __future__ import annotations

from enum import StrEnum


class EnumContextPackMode(StrEnum):
    """``pinned`` reads the manifest case named by the request (plan task T0.2)."""

    LIVE = "live"
    PINNED = "pinned"


__all__ = ["EnumContextPackMode"]
