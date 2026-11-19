# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Reasons a delegation step's engine order cannot be answered."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDelegationStepOrderRefusal(StrEnum):
    """Reasons a delegation step's engine order cannot be answered."""

    READER_REFUSED = "reader_refused"
    ENGINES_NOT_PLACED = "engines_not_placed"
    NO_ENGINE_ON_HOST = "no_engine_on_host"
    OVERLAY_INVALID = "overlay_invalid"
    ANSWER_SHAPE = "answer_shape"


__all__: list[str] = ["EnumDelegationStepOrderRefusal"]
