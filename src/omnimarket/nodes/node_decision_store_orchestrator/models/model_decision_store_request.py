# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input/output models for node_decision_store_orchestrator [OMN-12219].

Contains:
- EnumDecisionAction: record | query | check_conflicts
- EnumDecisionType: TECH_STACK_CHOICE | DESIGN_PATTERN | API_CONTRACT | SCOPE_BOUNDARY | REQUIREMENT_CHOICE
- EnumDecisionLayer: architecture | design | planning
- EnumConflictSeverity: LOW | MEDIUM | HIGH
- EnumConflictStatus: OPEN | RESOLVED | DISMISSED
- ModelDecisionStoreRequest: input to the orchestrator
- ModelDecisionStoreResult: output from the orchestrator
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDecisionAction(StrEnum):
    """Sub-operation for the decision store orchestrator."""

    RECORD = "record"
    QUERY = "query"
    CHECK_CONFLICTS = "check_conflicts"


class EnumDecisionType(StrEnum):
    """Category of architectural or design decision."""

    TECH_STACK_CHOICE = "TECH_STACK_CHOICE"
    DESIGN_PATTERN = "DESIGN_PATTERN"
    API_CONTRACT = "API_CONTRACT"
    SCOPE_BOUNDARY = "SCOPE_BOUNDARY"
    REQUIREMENT_CHOICE = "REQUIREMENT_CHOICE"


class EnumDecisionLayer(StrEnum):
    """Architectural layer the decision belongs to."""

    ARCHITECTURE = "architecture"
    DESIGN = "design"
    PLANNING = "planning"


class EnumConflictSeverity(StrEnum):
    """Severity of a detected decision conflict."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class EnumConflictStatus(StrEnum):
    """Resolution status of a decision conflict."""

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class ModelDecisionEntry(BaseModel):
    """A single architectural/design decision to record or check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_type: EnumDecisionType = Field(
        description="Category of the decision.",
    )
    domain: str = Field(
        description="Domain or subsystem this decision belongs to (e.g. 'routing', 'storage').",
    )
    layer: EnumDecisionLayer = Field(
        description="Architectural layer the decision belongs to.",
    )
    services: tuple[str, ...] = Field(
        default=(),
        description="Affected services. Empty tuple = platform-wide scope.",
    )
    summary: str = Field(
        description="One-line summary of the decision.",
    )
    rationale: str = Field(
        default="",
        description="Full rationale for the decision. Required for record; optional for check_conflicts.",
    )


class ModelConflictResult(BaseModel):
    """A detected conflict between two decisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: str = Field(
        description="Stable UUID identifying this conflict pair.",
    )
    entry_a_id: str = Field(
        description="ID of the first decision in the conflict pair.",
    )
    entry_b_id: str = Field(
        description="ID of the second decision in the conflict pair.",
    )
    structural_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Structural conflict confidence (0.0=cross-domain, 1.0=identical scope).",
    )
    severity: EnumConflictSeverity = Field(
        description="Computed conflict severity after applying modifier rules.",
    )
    status: EnumConflictStatus = Field(
        default=EnumConflictStatus.OPEN,
        description="Resolution status of this conflict.",
    )
    semantic_checked: bool = Field(
        default=False,
        description="Whether an async LLM semantic check was triggered.",
    )


class ModelDecisionQueryFilter(BaseModel):
    """Optional filters for the query sub-operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: str | None = Field(default=None, description="Filter by domain.")
    layer: EnumDecisionLayer | None = Field(
        default=None, description="Filter by layer."
    )
    decision_type: EnumDecisionType | None = Field(
        default=None, description="Filter by decision type."
    )
    service: str | None = Field(default=None, description="Filter by service name.")
    status: EnumConflictStatus | None = Field(
        default=None, description="Filter by conflict status."
    )
    cursor: str | None = Field(
        default=None, description="Cursor token for paginated results."
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of results to return.",
    )


class ModelDecisionStoreRequest(BaseModel):
    """Input to the decision store orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: EnumDecisionAction = Field(
        description="Sub-operation: record, query, or check_conflicts.",
    )
    entry: ModelDecisionEntry | None = Field(
        default=None,
        description="Decision entry to record or check. Required for record and check_conflicts.",
    )
    query_filter: ModelDecisionQueryFilter | None = Field(
        default=None,
        description="Query filters. Used only for query action.",
    )
    conflict_scope: str | None = Field(
        default=None,
        description=(
            "Optional domain scope override for conflict checking. "
            "Defaults to entry.domain when not specified."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="When true, validate and check conflicts but do not persist or notify.",
    )


class ModelDecisionStoreResult(BaseModel):
    """Output from the decision store orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: EnumDecisionAction = Field(
        description="Sub-operation that produced this result.",
    )
    stored_decision_id: str | None = Field(
        default=None,
        description="ID of the persisted decision entry. Set only on successful record.",
    )
    conflicts_found: tuple[ModelConflictResult, ...] = Field(
        default=(),
        description="All detected conflicts for this decision.",
    )
    high_severity_count: int = Field(
        default=0,
        description="Number of HIGH-severity conflicts detected.",
    )
    slack_gate_triggered: bool = Field(
        default=False,
        description="Whether the Slack conflict resolution gate was triggered.",
    )
    query_results: tuple[ModelDecisionEntry, ...] = Field(
        default=(),
        description="Paginated decision entries returned by a query action.",
    )
    next_cursor: str | None = Field(
        default=None,
        description="Cursor for the next page of query results.",
    )
    dry_run: bool = Field(
        description="Whether this was a dry-run (no writes or notifications).",
    )
