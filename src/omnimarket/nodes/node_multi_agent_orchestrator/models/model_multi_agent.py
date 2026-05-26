# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic models for node_multi_agent_orchestrator [OMN-12207].

All models are frozen value objects — no I/O, no LLM calls.

Workflow types (per multi_agent SKILL.md):
  parallel_debug   — fan out independent failure investigations to parallel agents
  parallel_build   — fan out independent implementation tasks to parallel agents
  sequential_review — chain tasks sequentially with code review between each

Input side:
  EnumWorkflowType       — workflow mode selector
  ModelAgentTask         — a single task for one agent
  ModelMultiAgentRequest — orchestrator input envelope (via handler module)

Output side:
  EnumAgentResultStatus  — per-agent execution outcome
  ModelAgentResult       — single agent result with status, findings, files changed
  EnumConflictClass      — geometric conflict classification for fan-in reconciliation
  ModelConflictField     — a field requiring approval due to competing values
  ModelReconciliation    — fan-in merge result: merged values + approval-required fields
  ModelMultiAgentResult  — aggregated findings across all agents
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EnumWorkflowType(StrEnum):
    """Multi-agent workflow mode (mirrors SKILL.md modes)."""

    PARALLEL_DEBUG = "parallel_debug"
    PARALLEL_BUILD = "parallel_build"
    SEQUENTIAL_REVIEW = "sequential_review"


class EnumAgentResultStatus(StrEnum):
    """Outcome of a single agent task execution."""

    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class EnumConflictClass(StrEnum):
    """Geometric conflict classification for fan-in reconciliation (per SKILL.md §5.1)."""

    NO_CONFLICT = "no_conflict"
    AUTO_MERGEABLE = "auto_mergeable"
    REQUIRES_APPROVAL = "requires_approval"


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class ModelAgentTask(BaseModel):
    """A single task dispatched to one agent in the workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(
        description="Stable identifier for this task (e.g. 't1', 't2')."
    )
    description: str = Field(description="Human-readable task description.")
    scope: list[str] = Field(
        default_factory=list,
        description="Files, modules, or subsystems in scope for this task.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "task_id values this task must wait for. Empty = no dependency "
            "(eligible for parallel dispatch)."
        ),
    )
    prompt_template: str | None = Field(
        default=None,
        description=(
            "Optional agent prompt template. When None, the orchestrator "
            "generates a prompt from description + scope."
        ),
    )
    validation_criteria: str | None = Field(
        default=None,
        description="Success criteria used in quality validation phase.",
    )


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class ModelAgentResult(BaseModel):
    """Result returned by a single agent after completing its task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(description="Matches the task_id from ModelAgentTask.")
    status: EnumAgentResultStatus
    summary: str = Field(description="Agent-provided summary of what was done.")
    files_changed: list[str] = Field(
        default_factory=list,
        description="List of file paths modified by this agent.",
    )
    findings: list[str] = Field(
        default_factory=list,
        description="Key findings, issues discovered, or actions taken.",
    )
    error: str | None = Field(
        default=None,
        description="Error message when status is FAILURE or TIMEOUT.",
    )


class ModelConflictField(BaseModel):
    """A single field where agent outputs conflict and require human approval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_path: str = Field(description="Dot-notation path to the conflicting field.")
    conflict_class: EnumConflictClass
    competing_values: dict[str, str] = Field(
        description="Map of agent_id → value for this field.",
    )
    chosen_value: str | None = Field(
        default=None,
        description=(
            "Auto-resolved value when conflict_class is AUTO_MERGEABLE. "
            "None when REQUIRES_APPROVAL — must be resolved by caller."
        ),
    )


class ModelReconciliation(BaseModel):
    """Fan-in reconciliation result for parallel workflows (per SKILL.md §5.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requires_approval: bool = Field(
        description=(
            "True when ≥1 field has conflict_class=REQUIRES_APPROVAL. "
            "Caller MUST surface competing values and halt auto-merge."
        )
    )
    merged_values: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Auto-resolved merged state. Only fields with NO_CONFLICT or "
            "AUTO_MERGEABLE are present. REQUIRES_APPROVAL fields are absent."
        ),
    )
    approval_required_fields: list[ModelConflictField] = Field(
        default_factory=list,
        description="Fields that cannot be auto-merged and need human resolution.",
    )
    optional_review_fields: list[ModelConflictField] = Field(
        default_factory=list,
        description="Fields where AUTO_MERGEABLE was applied but review is recommended.",
    )


class ModelMultiAgentResult(BaseModel):
    """Aggregated findings from a completed multi-agent workflow run."""

    model_config = ConfigDict(extra="forbid")

    workflow_type: EnumWorkflowType
    agent_results: list[ModelAgentResult] = Field(
        default_factory=list,
        description="Per-agent results in dispatch order.",
    )
    reconciliation: ModelReconciliation | None = Field(
        default=None,
        description=(
            "Fan-in reconciliation result. Present for parallel workflows; "
            "None for sequential_review (no overlap expected)."
        ),
    )
    succeeded_count: int = Field(ge=0, description="Number of agents that succeeded.")
    failed_count: int = Field(
        ge=0, description="Number of agents that failed or timed out."
    )
    skipped_count: int = Field(ge=0, description="Number of agents that were skipped.")
    total_files_changed: list[str] = Field(
        default_factory=list,
        description="Union of all files_changed across all agents (deduplicated).",
    )
    aggregated_findings: list[str] = Field(
        default_factory=list,
        description="Merged findings list from all agents, preserving attribution.",
    )
    approval_required: bool = Field(
        description=(
            "True when reconciliation.requires_approval=True. "
            "Orchestrator halts fan-in and emits approval-pending event."
        )
    )
