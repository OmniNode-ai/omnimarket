# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The health reading of the previous roll."""

from enum import StrEnum


class EnumDevRollHealth(StrEnum):
    """What the roll's health read says about the runtime it started."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"
