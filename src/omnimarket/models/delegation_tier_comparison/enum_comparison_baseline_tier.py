# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cheapest Claude tier that passes a class, or none."""

from __future__ import annotations

from enum import StrEnum


class EnumComparisonBaselineTier(StrEnum):
    """Closed set. ``none`` is a class with no passing baseline."""

    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"
    NONE = "none"
