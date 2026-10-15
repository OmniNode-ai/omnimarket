# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How an arm's cost per attempt was obtained."""

from __future__ import annotations

from enum import StrEnum


class EnumComparisonCostBasis(StrEnum):
    """Closed set: list price of a Claude arm, or a measured or estimated local cost."""

    LIST = "list"
    MEASURED_LOCAL = "measured_local"
    ESTIMATED_LOCAL = "estimated_local"
