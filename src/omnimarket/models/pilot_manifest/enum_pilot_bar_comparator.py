# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a measured value is held against a bar criterion's threshold."""

from __future__ import annotations

from enum import StrEnum


class EnumPilotBarComparator(StrEnum):
    GE = "ge"
    LE = "le"
    EQ = "eq"
