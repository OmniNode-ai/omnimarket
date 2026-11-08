# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The size-band rule for a paid rung (OMN-23180).

The measured size band decides whether a request may reach a rung that costs
money. The contract names the smallest band that may
(``escalation_policy.paid_from_size_band``); a request below it meets a paid
rung as a skip with cause ``size_band_below_paid``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.model_escalation_transitions import (
    ModelTaskClassTransitions,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation.model_transition_decision import (
    ModelTransitionDecision,
)


class ModelPaidRungRequest(BaseModel):
    """What the paid-rung rule reads: the rung's cost, the band and the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung_is_paid: bool
    size_band: EnumSizeBand | None = Field(
        default=None,
        description="The request's measured band; ``None`` when none was measured.",
    )
    paid_from_size_band: EnumSizeBand | None = Field(
        default=None,
        description="The contract's smallest band that may reach a paid rung; "
        "``None`` when the contract declares no such rule.",
    )
    transitions: ModelTaskClassTransitions


class ModelPaidRungDecision(BaseModel):
    """Whether the rung may be tried; a refusal carries the declared transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    transition: ModelTransitionDecision | None = None
    reason: str | None = None


__all__ = ["ModelPaidRungDecision", "ModelPaidRungRequest"]
