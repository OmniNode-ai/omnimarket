# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output model for the exploration policy compute node (OMN-12844 / M4).

The decision is fully auditable per the Context Authority Rule: it records the
selected identity, the per-candidate selection probability distribution, a
human-readable selection reason, the bandit family used, and the experiment
cohort. A forced selection is surfaced via ``EnumSelectionReason``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_exploration_policy_compute.models.model_exploration_policy_config import (
    EnumBanditFamily,
)


class EnumSelectionReason(StrEnum):
    """Why a particular candidate was selected."""

    EXPLOIT = "exploit"
    EXPLORE = "explore"
    COLD_START = "cold_start"
    EXPERIMENT_ASSIGNMENT = "experiment_assignment"


class ModelCandidateProbability(BaseModel):
    """The selection probability assigned to one candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capsule_hash: str = Field(
        min_length=1, description="Candidate capsule identity (sha256 hex)."
    )
    probability: float = Field(
        ge=0.0,
        le=1.0,
        description="Selection probability assigned to this candidate.",
    )
    effectiveness_score: float = Field(
        ge=0.0, le=1.0, description="Echoed raw effectiveness for audit."
    )
    decayed_confidence: float = Field(
        ge=0.0, le=1.0, description="Echoed decayed confidence ranked on."
    )
    hit_count: int = Field(ge=0, description="Echoed trial count for audit.")
    is_cold_start: bool = Field(
        description="True if below min_trials_before_exploit (floor applied)."
    )
    is_negative_control: bool = Field(
        description="True if this candidate is a negative control."
    )


class ModelExplorationDecision(BaseModel):
    """Output of the exploration policy: chosen candidate + full distribution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_capsule_hash: str = Field(
        min_length=1, description="Capsule identity chosen for this draw."
    )
    selection_reason: str = Field(
        min_length=1, description="Human-readable rationale for the selection."
    )
    selection_reason_class: EnumSelectionReason = Field(
        description="Typed selection reason class for audit/replay."
    )
    family: EnumBanditFamily = Field(
        description="Bandit family that produced the distribution."
    )
    experiment_cohort: str = Field(
        min_length=1, description="Cohort label echoed from the request."
    )
    candidate_probabilities: tuple[ModelCandidateProbability, ...] = Field(
        min_length=1,
        description="Per-candidate selection distribution (sums to 1.0).",
    )


__all__ = [
    "EnumSelectionReason",
    "ModelCandidateProbability",
    "ModelExplorationDecision",
]
