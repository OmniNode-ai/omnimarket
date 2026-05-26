# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic models for node_ticketing_epic_org_orchestrator [OMN-12202].

All models are frozen. No I/O, no LLM calls, no side effects in model layer.

Contains:
- ModelOrphanedTicket: a Linear ticket with no parent epic
- ModelProposedEpicGroup: a proposed grouping of tickets for a new epic
- ModelCreatedEpic: a Linear epic created by this orchestrator
- ModelEpicOrgGroupingDecision: classification verdict for a proposed group
- ModelTicketingEpicOrgRequest: input to the orchestrator
- ModelTicketingEpicOrgResult: output from the orchestrator
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelOrphanedTicket(BaseModel):
    """A Linear ticket that has no parent epic (orphaned).

    Sourced from a TriageReport YAML (ticketing_triage output) or fetched
    fresh from Linear when no triage report is provided.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Linear ticket identifier, e.g. OMN-1234")
    title: str = Field(description="Full ticket title as returned by Linear")
    repo: str | None = Field(
        default=None,
        description="Owning repository shortname inferred from label or branchName",
    )
    labels: list[str] = Field(
        default_factory=list,
        description="Linear labels attached to this ticket",
    )
    state: str = Field(default="", description="Linear workflow state name")
    priority: int | None = Field(
        default=None,
        description="Linear priority value (1=urgent, 4=low, 0=no priority)",
    )


class ModelProposedEpicGroup(BaseModel):
    """A proposed grouping of tickets that could become an epic.

    Produced by the grouping algorithm (prefix match, label match, or
    secondary clustering pass). Classification verdict determines whether
    the group is auto-created, gated on human approval, or refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_key: str = Field(
        description="Canonical key for this group, e.g. '(omniclaude, DB-SPLIT)'"
    )
    grouping_rule: str = Field(
        description="Rule that produced this group: prefix | label | secondary_cluster",
        pattern="^(prefix|label|secondary_cluster)$",
    )
    ticket_ids: list[str] = Field(
        description="Ordered list of ticket IDs in this group"
    )
    repo: str | None = Field(
        default=None,
        description="Single owning repo if all members share one; null for cross-repo",
    )
    prefix: str | None = Field(
        default=None,
        description="Named prefix extracted from ticket titles (e.g. 'DB-SPLIT')",
    )
    label: str | None = Field(
        default=None,
        description="Shared domain label (Rule 2 groups only)",
    )
    proposed_epic_title: str = Field(description="Human-readable proposed epic title")
    verdict: str = Field(
        description="Classification: auto_create | human_gate | structural_violation",
        pattern="^(auto_create|human_gate|structural_violation)$",
    )
    structural_violation_reason: str | None = Field(
        default=None,
        description="Reason for structural_violation verdict; null otherwise",
    )


class ModelCreatedEpic(BaseModel):
    """A Linear epic that was successfully created by this orchestrator run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    epic_id: str = Field(description="Linear identifier of the newly created epic")
    title: str = Field(description="Title of the created epic")
    children_linked: list[str] = Field(
        description="Ticket IDs reparented under this epic"
    )
    group_key: str = Field(
        description="The group_key of the ProposedEpicGroup that produced this epic"
    )


class ModelTicketingEpicOrgRequest(BaseModel):
    """Input envelope for the ticketing epic org orchestrator.

    Either triage_report_path (path to a TriageReport YAML from ticketing_triage)
    or orphaned_tickets (pre-fetched list) must be supplied. When both are absent,
    the orchestrator fetches orphans fresh from Linear.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    triage_report_path: str | None = Field(
        default=None,
        description="Filesystem path to a TriageReport YAML produced by ticketing_triage",
    )
    orphaned_tickets: list[ModelOrphanedTicket] = Field(
        default_factory=list,
        description="Pre-fetched orphaned ticket list; used when triage_report_path is None",
    )
    dry_run: bool = Field(
        default=False,
        description="When True, compute groupings and proposal but do not create epics",
    )
    auto_approve: bool = Field(
        default=False,
        description=(
            "When True, auto-create all auto_create-eligible groups without presenting "
            "the proposal for human confirmation"
        ),
    )
    run_id: str = Field(
        default="",
        description="Correlation run identifier for tracing this orchestrator invocation",
    )


class ModelTicketingEpicOrgResult(BaseModel):
    """Output envelope for the ticketing epic org orchestrator.

    Contains the full proposal (all proposed groups with verdicts), the list of
    epics actually created, and any structural violations that were refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(default="", description="Correlation run identifier")
    dry_run: bool = Field(
        default=False,
        description="True when this result was produced in dry-run mode",
    )
    orphaned_tickets_count: int = Field(
        ge=0,
        description="Total number of orphaned tickets processed",
    )
    proposed_groups: list[ModelProposedEpicGroup] = Field(
        default_factory=list,
        description="All proposed groups with verdicts (includes structural_violation groups)",
    )
    structural_violations: list[ModelProposedEpicGroup] = Field(
        default_factory=list,
        description="Groups refused due to structural_violation; subset of proposed_groups",
    )
    epics_created: list[ModelCreatedEpic] = Field(
        default_factory=list,
        description="Epics created during this run (empty in dry-run mode)",
    )
    human_gate_groups: list[ModelProposedEpicGroup] = Field(
        default_factory=list,
        description="Groups deferred for human approval",
    )
    tickets_reparented: int = Field(
        ge=0,
        default=0,
        description="Total number of tickets linked to a new or existing epic",
    )
