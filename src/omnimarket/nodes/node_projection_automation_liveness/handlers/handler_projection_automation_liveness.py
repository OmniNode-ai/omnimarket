# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold of the automation-liveness projection.

One seam event and the rows it can touch go in; the rows that event changes
come out. No database, no bus, no clock: every time in a row is a time an event
carried.

IDEMPOTENT AND ORDER-INDEPENDENT BY CONSTRUCTION
    The bus redelivers and the three producers of the seam (processes and
    observers, the watchdog, the delivery outbox) are not ordered against each
    other, so no field is a counter bumped per event. A run is keyed by its
    stable ``run_id`` and phase, and ``failures_in_window``,
    ``consecutive_idle_with_demand`` and ``open_run_started_at`` are recomputed
    from the process's run history, so a replayed run changes nothing. Times
    only move forward (a verdict older than the one held is ignored), delivery
    and record receipts keep the earliest success and the newest attempt, and
    an episode first seen from a receipt is a stub that its raise completes.

THE PROJECTION DOES NOT JUDGE
    The verdict, its reason and its state are the watchdog's, carried by its
    verdict events. A declared process that has not emitted has no verdict
    here until the watchdog states one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from omnimarket.models.liveness.model_automation_liveness import (
    FAILING_OUTCOMES,
    EnumAutomationRunOutcome,
    EnumAutomationRunPhase,
    ModelAutomationAlarmCleared,
    ModelAutomationAlarmDelivered,
    ModelAutomationAlarmRaised,
    ModelAutomationAlarmRecorded,
    ModelAutomationHeartbeat,
    ModelAutomationLivenessDeclared,
    ModelAutomationLivenessVerdictEvent,
    ModelAutomationRunObserved,
)
from omnimarket.nodes.node_projection_automation_liveness.models import (
    ModelAutomationAlarmEpisodeRow,
    ModelAutomationLivenessFoldRequest,
    ModelAutomationLivenessFoldResult,
    ModelAutomationLivenessSnapshot,
    ModelAutomationLivenessStateRow,
    ModelAutomationRunRow,
)

HANDLER_ID = "projection-automation-liveness"

#: Outcomes of a run that waited on demand without doing its work: it neither
#: failed nor worked. A failed run breaks the streak, it is not idleness.
_IDLE_OUTCOMES = frozenset(
    {EnumAutomationRunOutcome.OK, EnumAutomationRunOutcome.SKIPPED}
)


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _earlier(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _blank(process_id: str, host: str) -> ModelAutomationLivenessStateRow:
    return ModelAutomationLivenessStateRow(process_id=process_id, host=host)


def _changed(
    prior: ModelAutomationLivenessStateRow | None, new: ModelAutomationLivenessStateRow
) -> tuple[ModelAutomationLivenessStateRow, ...]:
    return () if prior == new else (new,)


def _run_row(event: ModelAutomationRunObserved) -> ModelAutomationRunRow:
    return ModelAutomationRunRow.model_validate(event.model_dump())


def _is_idle_with_demand(run: ModelAutomationRunRow) -> bool:
    return (
        run.outcome in _IDLE_OUTCOMES
        and run.did_work_count == 0
        and run.demand_count is not None
        and run.demand_count > 0
    )


def _state_from_runs(
    state: ModelAutomationLivenessStateRow,
    runs: list[ModelAutomationRunRow],
    window_seconds: int,
) -> ModelAutomationLivenessStateRow:
    """Recompute every run-derived field from the process's run history."""
    finished = sorted(
        (
            r
            for r in runs
            if r.phase is EnumAutomationRunPhase.FINISHED and r.finished_at is not None
        ),
        key=lambda r: (r.finished_at, r.run_id),
    )
    finished_ids = {r.run_id for r in finished}
    open_starts = [
        r.started_at
        for r in runs
        if r.phase is EnumAutomationRunPhase.STARTED and r.run_id not in finished_ids
    ]
    last_run_at = state.last_run_at
    for r in runs:
        last_run_at = _later(last_run_at, r.finished_at or r.started_at)
    update: dict[str, object] = {
        "last_run_at": last_run_at,
        "open_run_started_at": min(open_starts, default=None),
    }
    if finished:
        latest = finished[-1]
        assert latest.finished_at is not None
        horizon = latest.finished_at - timedelta(seconds=window_seconds)
        worked = [r for r in finished if (r.did_work_count or 0) > 0]
        streak = 0
        for r in reversed(finished):
            if not _is_idle_with_demand(r):
                break
            streak += 1
        update |= {
            "last_outcome": latest.outcome,
            "last_did_work_count": latest.did_work_count,
            "last_demand_count": latest.demand_count,
            "last_work_at": _later(
                state.last_work_at, worked[-1].finished_at if worked else None
            ),
            "failures_in_window": sum(
                1
                for r in finished
                if r.outcome in FAILING_OUTCOMES
                and r.finished_at is not None
                and r.finished_at > horizon
            ),
            "consecutive_idle_with_demand": streak,
        }
    return state.model_copy(update=update)


def _fold_declared(
    event: ModelAutomationLivenessDeclared, prior: ModelAutomationLivenessSnapshot
) -> ModelAutomationLivenessSnapshot:
    changed: list[ModelAutomationLivenessStateRow] = []
    for entry in event.overlay.processes:
        existing = prior.state(entry.process_id, entry.host)
        if (
            existing is not None
            and existing.declared_at is not None
            and event.declared_at < existing.declared_at
        ):
            continue
        base = existing or _blank(entry.process_id, entry.host)
        new = base.model_copy(
            update={
                "declared_at": event.declared_at,
                "process_state": entry.state,
                "contract_digest": entry.digest(),
            }
        )
        changed.extend(_changed(existing, new))
    return ModelAutomationLivenessSnapshot(states=tuple(changed))


def _fold_run(
    event: ModelAutomationRunObserved,
    prior: ModelAutomationLivenessSnapshot,
    window_seconds: int,
) -> ModelAutomationLivenessSnapshot:
    incoming = _run_row(event)
    held = next((r for r in prior.runs if r.key == incoming.key), None)
    kept = (
        held
        if held is not None and held.observed_at >= incoming.observed_at
        else incoming
    )
    history = [
        r for r in prior.runs_for(event.process_id, event.host) if r.key != kept.key
    ]
    history.append(kept)
    existing = prior.state(event.process_id, event.host)
    new = _state_from_runs(
        existing or _blank(event.process_id, event.host), history, window_seconds
    )
    if new.contract_digest is None:
        new = new.model_copy(update={"contract_digest": event.contract_digest})
    return ModelAutomationLivenessSnapshot(
        states=_changed(existing, new),
        runs=() if kept == held else (kept,),
    )


def _fold_heartbeat(
    event: ModelAutomationHeartbeat, prior: ModelAutomationLivenessSnapshot
) -> ModelAutomationLivenessSnapshot:
    existing = prior.state(event.process_id, event.host)
    state = existing or _blank(event.process_id, event.host)
    newest = (
        state.last_heartbeat_at is None or event.emitted_at >= state.last_heartbeat_at
    )
    update: dict[str, object] = {
        "last_heartbeat_at": _later(state.last_heartbeat_at, event.emitted_at),
        "last_progress_at": _later(state.last_progress_at, event.last_progress_at),
    }
    if newest:
        # The counter belongs to one process lifetime and restarts with it, so
        # the newest heartbeat's value stands rather than the larger one.
        update["progress_counter"] = event.progress_counter
        # A daemon has no runs; its demand is whatever its heartbeat reports.
        if state.last_run_at is None and event.demand_count is not None:
            update["last_demand_count"] = event.demand_count
    return ModelAutomationLivenessSnapshot(
        states=_changed(existing, state.model_copy(update=update))
    )


def _fold_verdict(
    event: ModelAutomationLivenessVerdictEvent, prior: ModelAutomationLivenessSnapshot
) -> ModelAutomationLivenessSnapshot:
    existing = prior.state(event.process_id, event.host)
    state = existing or _blank(event.process_id, event.host)
    if (
        state.verdict_evaluated_at is not None
        and event.evaluated_at < state.verdict_evaluated_at
    ):
        return ModelAutomationLivenessSnapshot()
    update: dict[str, object] = {
        "verdict": event.verdict,
        "verdict_reason": event.reason,
        "verdict_state": event.state,
        "verdict_since": event.verdict_since,
        "verdict_evaluated_at": event.evaluated_at,
    }
    if state.contract_digest is None and event.contract_digest is not None:
        update["contract_digest"] = event.contract_digest
    return ModelAutomationLivenessSnapshot(
        states=_changed(existing, state.model_copy(update=update))
    )


def _episode(
    prior: ModelAutomationLivenessSnapshot, episode_id: UUID
) -> ModelAutomationAlarmEpisodeRow:
    return prior.episode(episode_id) or ModelAutomationAlarmEpisodeRow(
        episode_id=episode_id
    )


def _fold_raised(
    event: ModelAutomationAlarmRaised, prior: ModelAutomationLivenessSnapshot
) -> ModelAutomationLivenessSnapshot:
    held = prior.episode(event.episode_id)
    episode = _episode(prior, event.episode_id).model_copy(
        update={
            "process_id": event.process_id,
            "host": event.host,
            "verdict": event.verdict,
            "state": event.state,
            "reason": event.reason,
            "severity": event.severity,
            "opened_at": event.opened_at,
            "delivery_due_at": event.delivery_due_at,
            "evidence_ref": event.evidence_ref,
            "action": event.action,
        }
    )
    existing = prior.state(event.process_id, event.host)
    state = existing or _blank(event.process_id, event.host)
    current = prior.episode(state.open_episode_id) if state.open_episode_id else None
    superseded = (
        current is not None
        and current.episode_id != event.episode_id
        and current.opened_at is not None
        and current.opened_at > event.opened_at
    )
    # An episode cleared before its raise was folded must not reopen.
    if episode.cleared_at is None and not superseded:
        state = state.model_copy(update={"open_episode_id": event.episode_id})
    return ModelAutomationLivenessSnapshot(
        states=_changed(existing, state),
        episodes=() if episode == held else (episode,),
    )


def _fold_cleared(
    event: ModelAutomationAlarmCleared, prior: ModelAutomationLivenessSnapshot
) -> ModelAutomationLivenessSnapshot:
    held = prior.episode(event.episode_id)
    base = _episode(prior, event.episode_id)
    episode = base.model_copy(
        update={
            "process_id": base.process_id or event.process_id,
            "host": base.host or event.host,
            "verdict": base.verdict or event.verdict,
            "opened_at": base.opened_at or event.opened_at,
            "cleared_at": _later(base.cleared_at, event.cleared_at),
        }
    )
    existing = prior.state(event.process_id, event.host)
    # A clear names no new process; it only closes the episode a row holds open.
    closes = existing is not None and existing.open_episode_id == event.episode_id
    return ModelAutomationLivenessSnapshot(
        states=(
            (existing.model_copy(update={"open_episode_id": None}),)
            if closes and existing is not None
            else ()
        ),
        episodes=() if episode == held else (episode,),
    )


def _fold_delivered(
    event: ModelAutomationAlarmDelivered, prior: ModelAutomationLivenessSnapshot
) -> ModelAutomationLivenessSnapshot:
    held = prior.episode(event.episode_id)
    base = _episode(prior, event.episode_id)
    update: dict[str, object] = {}
    if base.last_attempt_at is None or event.attempted_at >= base.last_attempt_at:
        update |= {
            "last_attempt_at": event.attempted_at,
            "last_attempt_route": event.route,
            "last_attempt_delivered": event.delivered,
            "last_attempt_failure": event.failure,
        }
    if event.delivered and (
        base.delivered_at is None or event.attempted_at < base.delivered_at
    ):
        update |= {
            "delivered_at": event.attempted_at,
            "delivery_route": event.route,
            "delivery_ref": event.message_ref,
        }
    episode = base.model_copy(update=update)
    return ModelAutomationLivenessSnapshot(
        episodes=() if episode == held else (episode,)
    )


def _fold_recorded(
    event: ModelAutomationAlarmRecorded, prior: ModelAutomationLivenessSnapshot
) -> ModelAutomationLivenessSnapshot:
    held = prior.episode(event.episode_id)
    base = _episode(prior, event.episode_id)
    update: dict[str, object] = {}
    if base.recorded_at is None or event.recorded_at < base.recorded_at:
        update |= {
            "recorded_at": event.recorded_at,
            "ledger_line": event.ledger_line,
            "recorded_by": event.recorded_by,
        }
    if base.last_recorded_at is None or event.recorded_at >= base.last_recorded_at:
        update |= {
            "last_recorded_at": event.recorded_at,
            "last_ledger_line": event.ledger_line,
        }
    episode = base.model_copy(update=update)
    return ModelAutomationLivenessSnapshot(
        episodes=() if episode == held else (episode,)
    )


class HandlerProjectionAutomationLiveness:
    """Folds one seam event into the rows it changes."""

    def handle(
        self, request: ModelAutomationLivenessFoldRequest
    ) -> ModelAutomationLivenessFoldResult:
        """Fold one event against its prior rows. Pure."""
        event = request.event
        prior = request.prior
        if isinstance(event, ModelAutomationLivenessDeclared):
            changes = _fold_declared(event, prior)
        elif isinstance(event, ModelAutomationRunObserved):
            changes = _fold_run(event, prior, request.failure_window_seconds)
        elif isinstance(event, ModelAutomationHeartbeat):
            changes = _fold_heartbeat(event, prior)
        elif isinstance(event, ModelAutomationLivenessVerdictEvent):
            changes = _fold_verdict(event, prior)
        elif isinstance(event, ModelAutomationAlarmRaised):
            changes = _fold_raised(event, prior)
        elif isinstance(event, ModelAutomationAlarmCleared):
            changes = _fold_cleared(event, prior)
        elif isinstance(event, ModelAutomationAlarmDelivered):
            changes = _fold_delivered(event, prior)
        else:
            changes = _fold_recorded(event, prior)
        return ModelAutomationLivenessFoldResult(changes=changes)


__all__ = ["HANDLER_ID", "HandlerProjectionAutomationLiveness"]
