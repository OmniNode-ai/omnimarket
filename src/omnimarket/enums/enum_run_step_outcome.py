# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""How one step of a delegation run ended."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumRunStepOutcome(StrEnum):
    """A step either did what it was for or did not; the cause is carried apart."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


__all__ = ["EnumRunStepOutcome"]
