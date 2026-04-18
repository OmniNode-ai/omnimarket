# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_sweep_outcome_classify [OMN-8963, OMN-8996].

EnumSweepOutcome: 9-value outcome classification (Phase 1 + Phase 2).
ModelSweepOutcomeInput: union input (one of the 6 completion event types).
ModelSweepOutcomeClassified: output with canonical outcome + metadata.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class EnumSweepOutcome(StrEnum):
    """Outcome of a single PR's polish attempt."""

    # Phase 1 outcomes
    MERGED = "merged"  # PR was fully merged (future: after lifecycle merge)
    ARMED = "armed"  # auto-merge was armed via GraphQL (merge pending)
    REBASED = "rebased"  # branch was rebased onto base (CI will re-run)
    CI_RERUN_TRIGGERED = "ci_rerun_triggered"  # CI rerun was triggered
    FAILED = "failed"  # effect attempted but reported failure
    STUCK = "stuck"  # conflict or unresolvable state, needs human
    # Phase 2 outcomes
    SUCCESS = "success"  # Phase 2 effect completed successfully (reply posted, conflict resolved, CI fix applied+tested)
    DEGRADED = "degraded"  # Phase 2 effect attempted but partially failed (reply not posted, patch failed tests, etc.)
    NOOP = "noop"  # Phase 2 effect determined no action was needed (is_noop=True)


class ModelSweepOutcomeInput(BaseModel):
    """Input to classify: one completion event from any of the 3 effect topics.

    The union is represented as a free-form dict because the 3 completion event
    types (armed / rebase / ci_rerun) have different shapes. The handler
    discriminates on the 'event_type' field.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    event_type: str  # "armed" | "rebase_completed" | "ci_rerun_triggered" | "thread_replied" | "conflict_resolved" | "ci_fix_attempted"
    pr_number: int
    repo: str
    correlation_id: UUID
    run_id: UUID
    total_prs: int
    # Phase 1 effect-specific fields
    # Armed:
    armed: bool | None = None
    # Rebase:
    success: bool | None = None
    conflict_files: list[str] = Field(default_factory=list)
    # CI rerun:
    rerun_triggered: bool | None = None
    # Phase 2 effect-specific fields
    # thread_replied:
    reply_posted: bool | None = None
    # conflict_resolved:
    resolution_committed: bool | None = None
    # ci_fix_attempted + conflict_resolved:
    is_noop: bool | None = None
    # ci_fix_attempted:
    patch_applied: bool | None = None
    local_tests_passed: bool | None = None
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
