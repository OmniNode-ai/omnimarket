# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure builders of the seam events node_github_schedule_observer_effect publishes (OMN-20803)."""

from __future__ import annotations

import re
from datetime import datetime

from omnibase_core.enums.enum_liveness_state import EnumLivenessState

from omnimarket.models.liveness.model_automation_liveness import (
    VERDICT_REASONS,
    VERDICT_STATE,
    EnumAutomationEmitter,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationRunOutcome,
    EnumAutomationRunPhase,
    ModelAutomationHeartbeat,
    ModelAutomationLivenessEntry,
    ModelAutomationLivenessVerdictEvent,
    ModelAutomationRunObserved,
)
from omnimarket.nodes.node_github_schedule_observer_effect.models.model_github_schedule_facts import (
    ModelGithubWorkflowRun,
)

_OUTCOMES: dict[str, EnumAutomationRunOutcome] = {
    "success": EnumAutomationRunOutcome.OK,
    "neutral": EnumAutomationRunOutcome.OK,
    "failure": EnumAutomationRunOutcome.FAILED,
    "startup_failure": EnumAutomationRunOutcome.FAILED,
    "timed_out": EnumAutomationRunOutcome.TIMEOUT,
    "action_required": EnumAutomationRunOutcome.DEGRADED,
    "cancelled": EnumAutomationRunOutcome.SKIPPED,
    "skipped": EnumAutomationRunOutcome.SKIPPED,
    "stale": EnumAutomationRunOutcome.SKIPPED,
}
_UNSAFE_PROCESS_CHARS = re.compile(r"[^a-z0-9._/-]")


def run_outcome(conclusion: str | None) -> EnumAutomationRunOutcome:
    """Map a completed run's conclusion; one GitHub does not name is a failure."""
    return _OUTCOMES.get(conclusion or "", EnumAutomationRunOutcome.FAILED)


def undeclared_process_id(repository: str, workflow_file: str) -> str:
    """The process id a scheduled workflow with no overlay entry is reported under."""
    name = repository.split("/", 1)[1]
    stem = workflow_file.rsplit(".", 1)[0]
    return _UNSAFE_PROCESS_CHARS.sub("-", f"github/{name}/{stem}".lower())


def run_id_of(entry: ModelAutomationLivenessEntry, run_key: str) -> str:
    return f"{entry.process_id}:{run_key}"


def started_event(
    entry: ModelAutomationLivenessEntry,
    *,
    run_key: str,
    started_at: datetime,
    work_unit: str | None,
    evidence_ref: str,
    observed_at: datetime,
) -> ModelAutomationRunObserved:
    return ModelAutomationRunObserved(
        process_id=entry.process_id,
        host=entry.host,
        run_id=run_id_of(entry, run_key),
        phase=EnumAutomationRunPhase.STARTED,
        started_at=started_at,
        work_unit=work_unit,
        evidence_ref=evidence_ref,
        emitter=EnumAutomationEmitter.OBSERVER,
        observed_at=observed_at,
        contract_digest=entry.digest(),
    )


def finished_event(
    entry: ModelAutomationLivenessEntry,
    run: ModelGithubWorkflowRun,
    *,
    evidence_ref: str,
    observed_at: datetime,
) -> ModelAutomationRunObserved:
    """A completed scheduled run. A run's own work is not on GitHub: an OK run counts one."""
    outcome = run_outcome(run.conclusion)
    started_at = run.run_started_at or run.created_at
    return ModelAutomationRunObserved(
        process_id=entry.process_id,
        host=entry.host,
        run_id=run_id_of(entry, str(run.id)),
        phase=EnumAutomationRunPhase.FINISHED,
        started_at=started_at,
        finished_at=max(run.updated_at, started_at),
        outcome=outcome,
        did_work_count=1 if outcome is EnumAutomationRunOutcome.OK else 0,
        evidence_ref=evidence_ref,
        emitter=EnumAutomationEmitter.OBSERVER,
        observed_at=observed_at,
        contract_digest=entry.digest(),
    )


def verdict_event(
    entry: ModelAutomationLivenessEntry | None,
    *,
    process_id: str,
    host: str,
    verdict: EnumAutomationLivenessVerdict,
    reason: EnumAutomationLivenessReason,
    detail: str,
    observed_at: datetime,
) -> ModelAutomationLivenessVerdictEvent:
    if reason not in VERDICT_REASONS[verdict]:
        raise ValueError(f"reason {reason.value} does not belong to {verdict.value}")
    state: EnumLivenessState = VERDICT_STATE[verdict]
    return ModelAutomationLivenessVerdictEvent(
        process_id=process_id,
        host=host,
        verdict=verdict,
        state=state,
        reason=reason,
        verdict_since=observed_at,
        evaluated_at=observed_at,
        contract_digest=None if entry is None else entry.digest(),
        detail=detail,
    )


def unobservable_event(
    entry: ModelAutomationLivenessEntry,
    reason: EnumAutomationLivenessReason,
    detail: str,
    *,
    observed_at: datetime,
) -> ModelAutomationLivenessVerdictEvent:
    return verdict_event(
        entry,
        process_id=entry.process_id,
        host=entry.host,
        verdict=EnumAutomationLivenessVerdict.UNOBSERVABLE,
        reason=reason,
        detail=detail,
        observed_at=observed_at,
    )


def heartbeat_event(
    *,
    process_id: str,
    host: str,
    process_started_at: datetime,
    ticks_completed: int,
    observed_at: datetime,
) -> ModelAutomationHeartbeat:
    return ModelAutomationHeartbeat(
        process_id=process_id,
        host=host,
        process_started_at=process_started_at,
        progress_counter=ticks_completed,
        last_progress_at=observed_at,
        emitted_at=observed_at,
    )
