# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Payload of the per-transition landing event (the projection's input)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.pr_landing.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.events.pr_landing.model_pr_landing_intent import (
    ModelPrLandingIntent,
)
from omnimarket.events.pr_landing.model_pr_landing_observation import (
    HEAD_SHA_PATTERN,
    REPOSITORY_PATTERN,
)


class ModelPrLandingTransitioned(BaseModel):
    """Emitted on every transition, with the per-key ``seq`` that orders them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(..., ge=1)
    head_sha: str | None = Field(default=None, pattern=HEAD_SHA_PATTERN)
    seq: int = Field(..., ge=0)
    from_state: EnumPrLandingState | None = Field(
        ..., description="None on the first transition of a row."
    )
    to_state: EnumPrLandingState = Field(...)
    trigger: str = Field(
        ..., min_length=1, description="The contract state_machine trigger taken."
    )
    intents: tuple[ModelPrLandingIntent, ...] = Field(default=())
    transitioned_at: datetime = Field(...)
    withheld_reason: str | None = Field(
        default=None,
        description=(
            "Why a green head was not armed on its ledger gate facts (OMN-20866): "
            "'<code>: <detail>', the code one of ledger_hold_in_force, "
            "ledger_holds_unknown, lab_pass_missing or lab_pass_unknown."
        ),
    )


__all__: list[str] = ["ModelPrLandingTransitioned"]
