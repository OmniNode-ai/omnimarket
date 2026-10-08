# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Sources of contracts evaluated by DoD verification (OMN-20696)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDodContractSource(StrEnum):
    """Where the evaluated contract came from."""

    PRODUCT_REPOSITORY = "product_repository"
    ONEX_CHANGE_CONTROL = "onex_change_control"
    INLINE_GOAL = "inline_goal"
    UNBOUND = "unbound"


__all__ = ["EnumDodContractSource"]
