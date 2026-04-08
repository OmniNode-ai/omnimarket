"""Pydantic models for the live build loop runner (assemble_live.py).

These models back the configuration, LLM dispatch, and per-ticket outcomes
produced by the live sub-handler implementations.

Related:
    - OMN-5113: Autonomous Build Loop epic
    - OMN-7823: Set up continuous build loop with verification
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelLlmEndpoint(BaseModel):
    """Configuration for a single LLM endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., description="Human-readable endpoint name.")
    url: str = Field(..., description="Base URL (OpenAI-compatible).")
    model_id: str = Field(..., description="Model ID to pass in API requests.")
    max_tokens: int = Field(default=4096, ge=1, description="Default max tokens.")
    timeout_seconds: float = Field(default=120.0, gt=0, description="Request timeout.")


class ModelLiveRunnerConfig(BaseModel):
    """Configuration for the live build loop runner.

    Captures all external dependencies: LLM endpoints, API keys,
    filesystem paths, and Linear team targeting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Linear
    linear_api_url: str = Field(
        default="https://api.linear.app/graphql",
        description="Linear GraphQL API URL.",
    )
    linear_team_id: str = Field(..., description="Linear team UUID to query.")
    linear_api_key_set: bool = Field(
        ..., description="Whether LINEAR_API_KEY is available (never store the key)."
    )

    # LLM endpoints
    classifier_endpoint: ModelLlmEndpoint = Field(
        ..., description="LLM endpoint for ticket classification."
    )
    coder_endpoint: ModelLlmEndpoint = Field(
        ..., description="LLM endpoint for code generation."
    )
    frontier_endpoint: ModelLlmEndpoint | None = Field(
        default=None,
        description="Optional frontier LLM endpoint (OpenAI/Google) for fallback.",
    )

    # Paths
    omni_home: str = Field(..., description="Path to omni_home repository registry.")
    worktree_root: str = Field(
        ..., description="Path to worktree root for feature branches."
    )

    # Execution
    max_cycles: int = Field(default=3, ge=1, description="Max build cycles.")
    max_tickets_per_cycle: int = Field(
        default=5, ge=1, description="Max tickets to process per cycle."
    )
    dry_run: bool = Field(default=False, description="Skip side effects.")
    skip_closeout: bool = Field(default=True, description="Skip CLOSING_OUT phase.")


class ModelLlmClassificationResult(BaseModel):
    """Result of LLM-based ticket classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket identifier.")
    buildability: str = Field(
        ...,
        description="Classification: auto_buildable, needs_arch_decision, blocked, skip.",
    )
    source: str = Field(
        ...,
        description="Classification source: llm_classifier, keyword_fallback.",
    )
    model_used: str = Field(
        default="", description="LLM model ID used for classification."
    )
    raw_response: str = Field(
        default="", description="Raw LLM response (truncated for storage)."
    )


class ModelDispatchOutcome(BaseModel):
    """Per-ticket outcome from the live build dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket identifier.")
    title: str = Field(default="", description="Ticket title.")
    target_repo: str = Field(default="", description="Target repository name.")
    branch_name: str = Field(default="", description="Git branch created.")
    worktree_path: str = Field(default="", description="Worktree filesystem path.")
    pr_url: str = Field(default="", description="GitHub PR URL if created.")
    files_written: int = Field(default=0, ge=0, description="Number of files written.")
    success: bool = Field(default=False, description="Whether dispatch completed.")
    error: str | None = Field(default=None, description="Error message if failed.")
    llm_model_used: str = Field(
        default="", description="LLM model used for code generation."
    )


class ModelLiveRunResult(BaseModel):
    """Complete result from a live build loop execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Root correlation ID.")
    started_at: datetime = Field(..., description="Execution start time.")
    completed_at: datetime = Field(..., description="Execution completion time.")
    config: ModelLiveRunnerConfig = Field(..., description="Configuration used.")
    cycles_completed: int = Field(default=0, ge=0)
    cycles_failed: int = Field(default=0, ge=0)
    total_tickets_dispatched: int = Field(default=0, ge=0)
    classifications: tuple[ModelLlmClassificationResult, ...] = Field(
        default_factory=tuple, description="Per-ticket classification results."
    )
    dispatch_outcomes: tuple[ModelDispatchOutcome, ...] = Field(
        default_factory=tuple, description="Per-ticket dispatch outcomes."
    )


__all__: list[str] = [
    "ModelDispatchOutcome",
    "ModelLiveRunResult",
    "ModelLiveRunnerConfig",
    "ModelLlmClassificationResult",
    "ModelLlmEndpoint",
]
