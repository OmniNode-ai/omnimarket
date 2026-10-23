# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Sources of contracts evaluated by DoD verification (OMN-20696)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDodContractSource(StrEnum):
    """Where the evaluated contract came from."""

    PRODUCT_REPOSITORY = "product_repository"
    INLINE_GOAL = "inline_goal"
    UNBOUND = "unbound"

    @classmethod
    def _missing_(cls, value: object) -> EnumDodContractSource | None:
        """Decode historical verdicts without retaining a separate live source.

        Repository, commit and path remain on the verdict; only the retired
        source category is normalized. New verdicts emit product_repository.
        """
        if value == "onex_change_control":
            return cls.PRODUCT_REPOSITORY
        return None


__all__ = ["EnumDodContractSource"]
