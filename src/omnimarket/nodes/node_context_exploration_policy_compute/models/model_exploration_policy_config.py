# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Contract-resolved bandit policy configuration (OMN-12844 / M4).

Every bandit parameter is carried on this frozen model and resolved FROM the
node contract config block -- never from module-level constants or env vars.
Switching the bandit family or epsilon is a contract/overlay edit, not a code
change. This model is the single authority for the exploration policy.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumBanditFamily(StrEnum):
    """Bandit selection family.

    EPSILON_GREEDY: with probability ``epsilon`` explore uniformly over the
        eligible pool; otherwise concentrate on the decayed-confidence argmax.
    UCB1: upper-confidence-bound selection -- exploit weight is the decayed
        confidence plus an uncertainty bonus that shrinks with trial count.
    THOMPSON: posterior sampling -- each candidate draws from a Beta posterior
        parameterised by its decayed confidence and trial count.
    """

    EPSILON_GREEDY = "epsilon_greedy"
    UCB1 = "ucb1"
    THOMPSON = "thompson"


class ModelExplorationPolicyConfig(BaseModel):
    """Typed bandit configuration resolved from the contract config block.

    All fields are required: there is no module-level default that could shadow
    the contract authority. The contract (or an overlay) supplies every value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: EnumBanditFamily = Field(description="Bandit selection family.")
    epsilon: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Exploration fraction. With probability epsilon the policy spreads "
            "mass uniformly across the eligible pool instead of exploiting."
        ),
    )
    ucb_c: float = Field(
        ge=0.0,
        description=(
            "UCB1 exploration coefficient scaling the uncertainty bonus. "
            "Higher = more weight on under-trialed candidates."
        ),
    )
    prior_alpha: float = Field(
        gt=0.0,
        description="Beta prior alpha (pseudo-successes) for Thompson sampling.",
    )
    prior_beta: float = Field(
        gt=0.0,
        description="Beta prior beta (pseudo-failures) for Thompson sampling.",
    )
    min_trials_before_exploit: int = Field(
        ge=0,
        description=(
            "A candidate with fewer than this many trials is treated as "
            "cold-start and guaranteed a nonzero exploration floor."
        ),
    )
    decay_half_life_days: float = Field(
        gt=0.0,
        description=(
            "Decay half-life (days) describing the staleness curve M2 applied. "
            "This policy does NOT recompute decay; the value documents the "
            "decay regime that produced the consumed decayed_confidence and "
            "informs the staleness audit reason only."
        ),
    )


__all__ = [
    "EnumBanditFamily",
    "ModelExplorationPolicyConfig",
]
