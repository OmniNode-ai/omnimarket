# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from enum import StrEnum


class EnumScoringMethod(StrEnum):
    EXACT_MATCH = "exact_match"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
