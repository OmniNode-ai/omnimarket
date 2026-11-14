# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate's typed answer for an input that yields neither facts nor a release."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dev_roll_gate_effect.models.enum_dev_roll_gate_observed_reason import (
    EnumDevRollGateObservedReason,
)


class ModelDevRollGateObserved(BaseModel):
    """Topic ``onex.evt.omnimarket.dev-roll-gate-observed.v1``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: EnumDevRollGateObservedReason
    correlation_id: str | None = None
    detail: str | None = None
