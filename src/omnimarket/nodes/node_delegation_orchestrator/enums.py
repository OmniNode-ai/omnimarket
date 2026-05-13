# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""FSM state enum for the delegation orchestrator."""

from __future__ import annotations

from enum import StrEnum


class EnumDelegationState(StrEnum):
    """FSM states for the delegation orchestrator."""

    RECEIVED = "RECEIVED"
    ROUTED = "ROUTED"
    EXECUTING = "EXECUTING"
    INFERENCE_COMPLETED = "INFERENCE_COMPLETED"
    GATE_EVALUATED = "GATE_EVALUATED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


__all__: list[str] = ["EnumDelegationState"]
