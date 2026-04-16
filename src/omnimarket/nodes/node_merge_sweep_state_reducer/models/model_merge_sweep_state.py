# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State models for node_merge_sweep_state_reducer [OMN-8964].

ModelMergeSweepState: aggregate state with first-writer-wins dedup.
ModelPrOutcomeRecord: per-PR record with terminal/transitional classification.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
)


class ModelPrOutcomeRecord(BaseModel):
    """First-writer-wins record per (pr_number, repo) within a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str  # "owner/name"
    outcome: EnumSweepOutcome
    terminal: (
        bool  # True if MERGED/FAILED/STUCK; False if ARMED/REBASED/CI_RERUN_TRIGGERED
    )
    is_noop: bool = False  # True if idempotent no-op success
    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    classified_event_id: UUID = Field(default_factory=uuid4)


# Outcomes that are "terminal" (no further action expected on the PR in this run)
_TERMINAL_OUTCOMES = {
    EnumSweepOutcome.MERGED,
    EnumSweepOutcome.FAILED,
    EnumSweepOutcome.STUCK,
}
# Outcomes that are "transitional" (PR may receive more events in this run)
_TRANSITIONAL_OUTCOMES = {
    EnumSweepOutcome.ARMED,
    EnumSweepOutcome.REBASED,
    EnumSweepOutcome.CI_RERUN_TRIGGERED,
}


def is_terminal_outcome(outcome: EnumSweepOutcome) -> bool:
    return outcome in _TERMINAL_OUTCOMES


class ModelMergeSweepState(BaseModel):
    """Aggregate sweep state. Dedup is a first-class contract property."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    total_prs: int
    # Canonical dedup map — key is f"{repo}#{pr_number}". First-writer-wins.
    # Duplicate classified events for the same PR DO NOT overwrite an existing record.
    pr_outcomes_by_key: dict[str, ModelPrOutcomeRecord] = Field(default_factory=dict)
    # Monotonic counters — incremented ONLY on first-write per dedup key.
    merged_count: int = 0
    armed_count: int = 0
    rebased_count: int = 0
    ci_rerun_count: int = 0
    failed_count: int = 0
    stuck_count: int = 0
    unresolvable_count: int = 0
    # Terminal emission guard — set to True the instant the reducer emits the
    # terminal event. Prevents double-emission under event-replay or rerun.
    terminal_emitted: bool = False
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
