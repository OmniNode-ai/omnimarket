# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_sweep_outcome_classify [OMN-8963].

EnumSweepOutcome: 6-value outcome classification.
ModelSweepOutcomeInput: union input (one of the 3 completion event types).
ModelSweepOutcomeClassified: output with canonical outcome + metadata.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class EnumSweepOutcome(StrEnum):
    """Outcome of a single PR's polish attempt."""

    MERGED = "merged"  # PR was fully merged (future: after lifecycle merge)
    ARMED = "armed"  # auto-merge was armed via GraphQL (merge pending)
    REBASED = "rebased"  # branch was rebased onto base (CI will re-run)
    CI_RERUN_TRIGGERED = "ci_rerun_triggered"  # CI rerun was triggered
    FAILED = "failed"  # effect attempted but reported failure
    STUCK = "stuck"  # conflict or unresolvable state, needs human


class ModelSweepOutcomeInput(BaseModel):
    """Input to classify: one completion event from any of the 3 effect topics.

    The union is represented as a free-form dict because the 3 completion event
    types (armed / rebase / ci_rerun) have different shapes. The handler
    discriminates on the 'event_type' field.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    event_type: str  # "armed" | "rebase_completed" | "ci_rerun_triggered"
    pr_number: int
    repo: str
    correlation_id: UUID
    run_id: UUID
    total_prs: int
    # Effect-specific fields — present depending on event_type
    # Armed:
    armed: bool | None = None
    # Rebase:
    success: bool | None = None
    conflict_files: list[str] = Field(default_factory=list)
    # CI rerun:
    rerun_triggered: bool | None = None
    # Shared error field
    error: str | None = None
    # Optional metadata
    extra: dict[str, Any] = Field(default_factory=dict)


class ModelSweepOutcomeClassified(BaseModel):
    """Classified outcome for a single PR. Emitted to pr-polish-outcome.v1 topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    pr_number: int
    repo: str
    correlation_id: UUID
    run_id: UUID
    total_prs: int
    outcome: EnumSweepOutcome
    source_event_type: str  # original event_type from input
    error: str | None = None
    conflict_files: list[str] = Field(default_factory=list)
