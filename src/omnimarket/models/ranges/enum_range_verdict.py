# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict of one range evaluation."""

from __future__ import annotations

from enum import StrEnum


class EnumRangeVerdict(StrEnum):
    """MET passes. MISSED and REFUSED both block.

    MISSED: the evaluation was valid and the one-sided lower bound did not clear
    the floor. REFUSED: the evaluation is not a valid range result at all (n
    below the power-analysed size, a pinned seed, a forced temperature, a
    retry-until-green run, or no samples).
    """

    MET = "met"
    MISSED = "missed"
    REFUSED = "refused"
