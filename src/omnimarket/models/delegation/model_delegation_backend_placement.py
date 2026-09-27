# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tier-ladder placement of a lane-added delegation backend (OMN-19215).

A lane overlay may ADD a backend, but routing only offers a backend that
``routing_tiers.yaml`` names in a tier's ``models``. A ``placement`` on the
backend's entry in the rendered bifrost contract names the tier and the rungs
the added backend is a fallback for; the routing authority appends one mirrored
tier entry per rung AFTER the tier's existing models when it loads the ladder.

These models live outside the wire package on purpose. A placement is routing
configuration read once at load, never a payload a consumer decodes, and the
bifrost config loader lifts it off the backend entry before the wire model
validates the rest, so ``ModelDelegationBackendConfig`` keeps the shape every
released consumer already accepts.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDelegationBackendPlacement(BaseModel):
    """Where an added backend sits in the routing tier ladder."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tier: str = Field(
        ..., min_length=1, description="Name of the routing tier to join."
    )
    fallback_for: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description=(
            "backend_refs of rungs already in that tier. Each yields one "
            "mirrored entry carrying the rung's use_for."
        ),
    )
    max_context_tokens: int = Field(
        ...,
        ge=1,
        description=(
            "Largest prompt this backend is offered. A mirrored entry takes the "
            "smaller of this and the rung's own max_context_tokens."
        ),
    )


class ModelPlacedDelegationBackend(BaseModel):
    """A bifrost backend that declares a placement, as the loader lifted it."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    backend_id: str = Field(..., min_length=1)
    model_name: str = Field(
        default="",
        description=(
            "The backend's served model id; a mirrored entry carries it. Empty "
            "is refused when the placement is applied, naming the backend."
        ),
    )
    placement: ModelDelegationBackendPlacement


__all__: list[str] = [
    "ModelDelegationBackendPlacement",
    "ModelPlacedDelegationBackend",
]
