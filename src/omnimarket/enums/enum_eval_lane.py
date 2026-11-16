# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime lane a scored try was scored on (OMN-24170)."""

from __future__ import annotations

from enum import StrEnum


class EnumEvalLane(StrEnum):
    """The lane that scored the try."""

    DEV = "dev"
    STABILITY = "stability"


__all__ = ["EnumEvalLane"]
