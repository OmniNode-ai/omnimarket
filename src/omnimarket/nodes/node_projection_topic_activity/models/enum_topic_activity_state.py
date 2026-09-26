# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator-facing activity state for a broker topic."""

from enum import StrEnum


class EnumTopicActivityState(StrEnum):
    ACTIVE = "ACTIVE"
    QUIET = "QUIET"
    UNKNOWN = "UNKNOWN"


__all__ = ["EnumTopicActivityState"]
