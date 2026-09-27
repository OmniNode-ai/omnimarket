# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outcome a companion command reports (rows 9 to 13 of the revision)."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingCompanionOutcome(StrEnum):
    """MINTED, DECLINED or ERROR, for the derive or regenerate command in flight."""

    MINTED = "minted"
    DECLINED = "declined"
    ERROR = "error"


__all__: list[str] = ["EnumPrLandingCompanionOutcome"]
