# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readiness scenarios an eval can feed (delegation readiness suite plan, section 2.3)."""

from __future__ import annotations

from enum import StrEnum


class EnumReadinessScenario(StrEnum):
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"
    R5 = "R5"
    R6 = "R6"
    R7 = "R7"
    R8 = "R8"
    R9 = "R9"
    EXPERIMENT = "experiment"
