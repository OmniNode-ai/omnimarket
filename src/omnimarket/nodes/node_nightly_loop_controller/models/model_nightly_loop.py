"""Pydantic models for the nightly loop controller node.

Backs contract.yaml inputs/outputs. Provides persistent config, decision
records, iteration results, and delegation routing rules.

Related:
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class DecisionOutcome(StrEnum):
    """Outcome of a nightly loop decision."""

    success = "success"
    failure = "failure"
    skipped = "skipped"
    deferred = "deferred"


class GapStatus(StrEnum):
    """Status of a tracked gap."""

    open = "open"
    in_progress = "in_progress"
    closed = "closed"
    blocked = "blocked"


class ModelDelegationRoute(BaseModel):
    """Routing rule: task_type -> model endpoint.

    Maps mechanical task types to local LLM endpoints and frontier tasks
    to Claude/OpenAI, enabling cost-aware delegation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_type: str = Field(
        ...,
        description="Task type pattern (e.g. 'test-fix', 'import-fix', 'refactor').",
    )
    model_endpoint: str = Field(..., description="LLM endpoint URL for this task type.")
    model_id: str = Field(..., description="Model ID at the endpoint.")
    cost_per_call_usd: float = Field(
        default=0.0, ge=0.0, description="Estimated cost per call in USD."
    )
    max_context_tokens: int = Field(
        default=4096, ge=1, description="Max context window for this route."
    )
    is_frontier: bool = Field(
        default=False,
        description="True if this is a frontier model (Claude, GPT-4, etc.).",
    )


class ModelNightlyLoopConfig(BaseModel):
    """Persistent config for the nightly loop controller.

    Stored in DB, not a markdown file. Defines priorities, routing table,
    and standing rules for autonomous nightly execution.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_id: UUID = Field(default_factory=uuid4, description="Config version ID.")
    priorities: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Ordered priority list (e.g. 'golden-chain-coverage', 'tech-debt').",
    )
    routing_table: tuple[ModelDelegationRoute, ...] = Field(
        default_factory=tuple,
        description="Delegation routing rules mapping task types to endpoints.",
    )
    active_gaps: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Active gap IDs being tracked for closure.",
    )
    standing_rules: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Standing rules applied every iteration (e.g. 'always run merge-sweep').",
    )
    max_iterations_per_run: int = Field(
        default=10, ge=1, description="Max iterations per nightly run."
    )
    max_cost_usd_per_run: float = Field(
        default=5.0, ge=0.0, description="Cost ceiling per run in USD."
    )


class ModelNightlyLoopDecision(BaseModel):
    """Individual decision record from a nightly loop iteration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: UUID = Field(default_factory=uuid4, description="Unique decision ID.")
    iteration_id: UUID = Field(..., description="Parent iteration ID.")
    correlation_id: UUID = Field(..., description="Root correlation ID for the run.")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        description="When the decision was made.",
    )
    action: str = Field(
        ..., description="What action was taken (e.g. 'dispatch-ticket', 'close-gap')."
    )
    target: str = Field(
        ..., description="Target of the action (ticket ID, gap ID, etc.)."
    )
    outcome: DecisionOutcome = Field(..., description="Outcome of the decision.")
    model_used: str = Field(
        default="", description="LLM model used for the decision, if any."
    )
    cost_usd: float = Field(
        default=0.0, ge=0.0, description="Cost of this decision in USD."
    )
    details: str = Field(default="", description="Additional details or error message.")


class ModelNightlyLoopIteration(BaseModel):
    """Record of a single nightly loop iteration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    iteration_id: UUID = Field(
        default_factory=uuid4, description="Unique iteration ID."
    )
    correlation_id: UUID = Field(..., description="Root correlation ID for the run.")
    iteration_number: int = Field(..., ge=1, description="1-based iteration index.")
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        description="When this iteration started.",
    )
    completed_at: datetime | None = Field(
        default=None, description="When this iteration completed."
    )
    gaps_checked: int = Field(default=0, ge=0, description="Number of gaps checked.")
    gaps_closed: int = Field(default=0, ge=0, description="Number of gaps closed.")
    decisions_made: int = Field(
        default=0, ge=0, description="Decisions made this iteration."
    )
    tickets_dispatched: int = Field(
        default=0, ge=0, description="Tickets dispatched this iteration."
    )
    total_cost_usd: float = Field(
        default=0.0, ge=0.0, description="Total cost for this iteration."
    )
    error: str | None = Field(
        default=None, description="Error message if iteration failed."
    )


class ModelNightlyLoopResult(BaseModel):
    """Result of a complete nightly loop run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Root correlation ID.")
    started_at: datetime = Field(..., description="Run start time.")
    completed_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        description="Run completion time.",
    )
    iterations_completed: int = Field(default=0, ge=0)
    iterations_failed: int = Field(default=0, ge=0)
    total_decisions: int = Field(default=0, ge=0)
    total_tickets_dispatched: int = Field(default=0, ge=0)
    total_gaps_checked: int = Field(default=0, ge=0)
    total_gaps_closed: int = Field(default=0, ge=0)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    gap_status: dict[str, GapStatus] = Field(
        default_factory=dict,
        description="Status of each tracked gap at run completion.",
    )
    iterations: tuple[ModelNightlyLoopIteration, ...] = Field(
        default_factory=tuple,
        description="Per-iteration details.",
    )
    decisions: tuple[ModelNightlyLoopDecision, ...] = Field(
        default_factory=tuple,
        description="All decisions made during the run.",
    )


def build_default_routing_table() -> tuple[ModelDelegationRoute, ...]:
    """Return the default delegation routing table for the nightly loop.

    Includes a gemini-cli route for architecture and multi-file refactor tasks,
    preferred over the local coder for those task types.

    Callers may extend or replace this table via ModelNightlyLoopConfig.routing_table.
    """
    import os

    gemini_endpoint = os.environ.get("GEMINI_CLI_ENDPOINT", "cli://gemini")
    local_coder_endpoint = os.environ.get(
        "LLM_CODER_URL",
        "http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-10580 reason="env-var fallback to lab coder endpoint; override via LLM_CODER_URL"
    )
    local_fast_endpoint = os.environ.get(
        "LLM_CODER_FAST_URL",
        "http://192.168.86.201:8001",  # onex-allow-internal-ip OMN-10580 reason="env-var fallback to lab fast endpoint; override via LLM_CODER_FAST_URL"
    )

    return (
        ModelDelegationRoute(
            task_type="architecture",
            model_endpoint=gemini_endpoint,
            model_id="gemini-cli",
            cost_per_call_usd=0.0,
            max_context_tokens=1000000,
            is_frontier=True,
        ),
        ModelDelegationRoute(
            task_type="multi-file",
            model_endpoint=gemini_endpoint,
            model_id="gemini-cli",
            cost_per_call_usd=0.0,
            max_context_tokens=1000000,
            is_frontier=True,
        ),
        ModelDelegationRoute(
            task_type="refactor",
            model_endpoint=local_coder_endpoint,
            model_id="default",
            cost_per_call_usd=0.0,
            max_context_tokens=64000,
            is_frontier=False,
        ),
        ModelDelegationRoute(
            task_type="classification",
            model_endpoint=local_fast_endpoint,
            model_id="default",
            cost_per_call_usd=0.0,
            max_context_tokens=40000,
            is_frontier=False,
        ),
    )


__all__: list[str] = [
    "DecisionOutcome",
    "GapStatus",
    "ModelDelegationRoute",
    "ModelNightlyLoopConfig",
    "ModelNightlyLoopDecision",
    "ModelNightlyLoopIteration",
    "ModelNightlyLoopResult",
    "build_default_routing_table",
]
