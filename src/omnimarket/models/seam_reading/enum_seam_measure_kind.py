# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a measure is reconciled: every handed-over id reported (transit), or every required id
present in the inventory (coverage)."""

from __future__ import annotations

from enum import StrEnum


class EnumSeamMeasureKind(StrEnum):
    TRANSIT = "transit"
    COVERAGE = "coverage"
