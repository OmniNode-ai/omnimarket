# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The kinds of step one delegation run passes through."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumRunStepKind(StrEnum):
    """What a step of a run was, in the order a run can reach them."""

    REQUEST = "request"
    ROUTING_DECISION = "routing_decision"
    PROVIDER_CALL = "provider_call"
    QUALITY_GATE = "quality_gate"
    TERMINAL = "terminal"


__all__ = ["EnumRunStepKind"]
