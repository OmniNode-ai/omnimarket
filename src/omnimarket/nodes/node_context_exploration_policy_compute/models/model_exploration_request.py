# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input model for the exploration policy compute node (OMN-12844 / M4).

The request carries the already-scored, already-decayed candidate set, the
contract-resolved bandit config, a typed RNG seed (so the handler stays pure),
and a typed ``now`` for staleness reasoning (no ``datetime.now()`` anywhere).

This node CONSUMES M2's decayed effectiveness/confidence; it does NOT recompute
decay and it does NOT import the M2 capsule-store private models. The candidate
identity is carried as a plain ``capsule_hash`` string so this node stays
decoupled from the projection's persistence models.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_context_exploration_policy_compute.models.model_exploration_policy_config import (
    ModelExplorationPolicyConfig,
)


class ModelExplorationCandidate(BaseModel):
    """One scored, decay-adjusted candidate the policy ranks over.

    ``effectiveness_score`` and ``decayed_confidence`` arrive already computed by
    the M2 capsule store / ROI scorer. ``decayed_confidence`` is the staleness-
    adjusted field this policy ranks on -- as it drops the candidate loses
    exploit weight and re-enters exploration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capsule_hash: str = Field(
        min_length=1,
        description="Stable capsule identity (sha256 hex) from the M2 store.",
    )
    effectiveness_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Raw learned effectiveness (e.g. first-pass / success rate).",
    )
    decayed_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Staleness-decayed confidence from M2. The policy ranks on this "
            "field and never recomputes decay."
        ),
    )
    hit_count: int = Field(
        ge=0,
        description="Number of scored trials folded into this candidate.",
    )
    last_scored: datetime = Field(
        description="Timestamp of the most recent scored event (tz-aware UTC).",
    )
    is_negative_control: bool = Field(
        default=False,
        description=(
            "True = waste/negative-control candidate. Never the exploit winner; "
            "sampled only when the experiment cohort forces it."
        ),
    )

    @field_validator("last_scored")
    @classmethod
    def _validate_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_scored must be timezone-aware")
        return value.astimezone(UTC)


class ModelExplorationRequest(BaseModel):
    """Input to the exploration policy compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[ModelExplorationCandidate, ...] = Field(
        min_length=1,
        description="Scored, decay-adjusted candidates to select among.",
    )
    config: ModelExplorationPolicyConfig = Field(
        description="Contract-resolved bandit policy configuration.",
    )
    seed: int = Field(
        ge=0,
        description=(
            "Typed RNG seed. The handler derives all randomness from this seed "
            "so the decision is deterministic and replayable -- no ambient "
            "random() state."
        ),
    )
    now: datetime = Field(
        description=(
            "Typed reference timestamp for staleness reasoning (tz-aware UTC). "
            "No datetime.now() is ever called inside the handler."
        ),
    )
    experiment_cohort: str = Field(
        min_length=1,
        description="Cohort label recorded on the decision for audit.",
    )

    @field_validator("now")
    @classmethod
    def _validate_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return value.astimezone(UTC)


__all__ = [
    "ModelExplorationCandidate",
    "ModelExplorationRequest",
]
