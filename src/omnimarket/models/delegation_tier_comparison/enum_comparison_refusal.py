# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why the comparison records of a run were refused."""

from __future__ import annotations

from enum import StrEnum


class EnumComparisonRefusal(StrEnum):
    """Closed set. A refused run publishes no record."""

    NO_REQUIREMENT = "no_requirement"
    DUPLICATE_ARM = "duplicate_arm"
    DUPLICATE_ITEM = "duplicate_item"
    EMPTY_ARM = "empty_arm"
    MIXED_MAPPED_TIER = "mixed_mapped_tier"
