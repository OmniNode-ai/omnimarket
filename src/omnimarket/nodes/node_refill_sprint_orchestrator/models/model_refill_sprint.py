# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic models for node_refill_sprint_orchestrator [OMN-12203].

All models are frozen value objects — no I/O, no LLM calls.

Input side:
  ModelSprintCapacityConfig  — capacity threshold + batch limits
  ModelBacklogFilter         — which tickets are eligible for pull
  ModelPriorityWeights       — how to score tier-1/2/3 candidates

Output side:
  ModelPulledTicket          — a single ticket that was moved to Active Sprint
  ModelSkippedTicket         — a ticket evaluated but not pulled, with reason
  ModelRefillSprintResult    — full run result: pulled, skipped, exhausted flag
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Tier / label constants (mirrors SKILL.md algorithm)
# ---------------------------------------------------------------------------


class ModelSprintCapacityConfig(BaseModel):
    """Sprint capacity configuration controlling when a refill is triggered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    threshold: float = Field(
        default=5.0,
        ge=0.0,
        description=(
            "Weighted capacity threshold. If the sum of estimate weights for "
            "Active Sprint tickets in Backlog/Todo state (no active PR) is at "
            "or above this value, the refill is skipped."
        ),
    )
    batch_size: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of tickets to pull per invocation.",
    )
    dry_run: bool = Field(
        default=False,
        description="When true, report what would be pulled without moving any tickets.",
    )
    skip_scope_check: bool = Field(
        default=False,
        description=(
            "Skip Phase 3 scope verification. Faster but risks pulling tickets "
            "whose file/API references no longer exist."
        ),
    )


class ModelBacklogFilter(BaseModel):
    """Filter criteria for Future-backlog candidate selection.

    Hard gates (always applied, per SKILL.md):
      - Estimate > Medium → excluded
      - Priority = Urgent → excluded
      - Has cross-repo child/blocker links → excluded
      - Has ≥2 failed implementation attempts ([auto-pull-attempt] comments) → excluded
      - Returned to Future today → excluded
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    team_id: str | None = Field(
        default=None,
        description="Restrict candidate selection to this Linear team ID.",
    )
    project_id: str | None = Field(
        default=None,
        description=(
            "Override the Future project ID. When None, the orchestrator "
            "resolves the canonical Future project for the team."
        ),
    )
    exclude_labels: list[str] = Field(
        default_factory=list,
        description="Additional label slugs to exclude from candidates.",
    )
    include_tier3_keywords: bool = Field(
        default=True,
        description=(
            "Include Tier 3 candidates matched by tech-debt keywords "
            "(tech debt, cleanup, refactor, dead code, deprecated)."
        ),
    )


class ModelPriorityWeights(BaseModel):
    """Scoring weights for candidate priority tiers.

    Tier 1 (type-suppression, lint-suppression, any-type-narrowing, skipped-tests)
    scores highest; Tier 3 (keyword-matched) scores lowest. Weights must be >= 0.
    The orchestrator normalizes weights; they do not need to sum to 1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_1: float = Field(
        default=3.0,
        ge=0.0,
        description="Relative weight for Tier 1 (suppression / skipped-test) tickets.",
    )
    tier_2: float = Field(
        default=2.0,
        ge=0.0,
        description="Relative weight for Tier 2 (friction-labelled) tickets.",
    )
    tier_3: float = Field(
        default=1.0,
        ge=0.0,
        description="Relative weight for Tier 3 (keyword-matched tech-debt) tickets.",
    )
    recency_boost: float = Field(
        default=0.5,
        ge=0.0,
        description=(
            "Extra weight added per week since the ticket was created "
            "(older tickets accumulate a small priority boost)."
        ),
    )


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class ModelPulledTicket(BaseModel):
    """A single ticket that was successfully moved to Active Sprint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Linear ticket identifier (e.g. OMN-1234).")
    title: str = Field(description="Ticket title at time of pull.")
    tier: int = Field(ge=1, le=3, description="Priority tier (1=highest, 3=lowest).")
    priority_score: float = Field(
        ge=0.0, description="Computed priority score used for ordering."
    )
    estimate_label: str | None = Field(
        default=None,
        description="Linear estimate label (No estimate, Small, Medium).",
    )
    scope_verified: bool = Field(
        description="True if Phase 3 scope verification passed or was skipped."
    )


class ModelSkippedTicket(BaseModel):
    """A ticket evaluated during candidate selection but not pulled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Linear ticket identifier.")
    title: str = Field(description="Ticket title.")
    reason: str = Field(
        description=(
            "Human-readable skip reason, e.g. 'estimate_too_large', "
            "'cross_repo_dependency', 'zombie_ticket', 'stale_scope', "
            "'returned_today', 'batch_limit_reached'."
        )
    )


class ModelRefillSprintResult(BaseModel):
    """Full result of a sprint refill run."""

    model_config = ConfigDict(extra="forbid")

    pulled: list[ModelPulledTicket] = Field(
        default_factory=list,
        description="Tickets successfully moved to Active Sprint.",
    )
    skipped: list[ModelSkippedTicket] = Field(
        default_factory=list,
        description="Tickets evaluated but not pulled, with reasons.",
    )
    pulled_count: int = Field(
        ge=0, description="Number of tickets moved to Active Sprint."
    )
    skipped_count: int = Field(ge=0, description="Number of tickets skipped.")
    exhausted: bool = Field(
        description="True when no eligible tech-debt tickets remain in Future."
    )
    capacity_before: float = Field(
        ge=0.0,
        description="Weighted sprint capacity before the pull (sum of estimate weights).",
    )
    capacity_after: float = Field(
        ge=0.0,
        description="Weighted sprint capacity after the pull.",
    )
    dry_run: bool = Field(
        description="True when the run was executed in dry-run mode (no mutations)."
    )
