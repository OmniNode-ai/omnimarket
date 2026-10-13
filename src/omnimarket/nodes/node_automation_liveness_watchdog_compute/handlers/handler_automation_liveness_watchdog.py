# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAutomationLivenessWatchdog -- verdicts, episodes and hysteresis (OMN-20806).

Pure: the effect that ticks this node folds the bus, persists the returned
state and delivers the returned alarms. One evaluation runs in this order.

1. **Self-test first.** The planted contracts go through the same evaluator
   as the real processes. A wrong answer voids the evaluation: no real verdict,
   episode or state change leaves it, and WATCHDOG_SELFTEST_FAILED opens at
   once. A watchdog that cannot judge a planted MISSED is not believed about a
   real one.
2. **Raw verdicts.** Every declared process, every undeclared process an
   observer reported, every host observer (HOST_SILENT), the bus (BUS_SILENT)
   and the leg itself (the self-test).
3. **Folding, on monitor loss only.** A silent host observer folds the MISSED
   verdicts of its host into one HOST_SILENT; a silent bus folds every failing
   verdict into one BUS_SILENT. A host whose observer is alive keeps every
   per-process verdict, however many are failing: a count of MISSED processes
   never folds into anything.
4. **Episodes.** A verdict opens its episode after the confirmation hold; a
   failed run, an undeclared process and the self-test open at once. An episode
   closes after the hold's count of passing evaluations, or one healthy run for
   a cadence of an hour or slower. A reason change inside the episode updates it
   silently; a severity rise re-raises it under the same episode id.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from omnimarket.models.liveness.model_automation_liveness import (
    LOW_SEVERITY_REASONS,
    PASSING_VERDICTS,
    VERDICT_STATE,
    EnumAlarmSeverity,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationProcessState,
    EnumAutomationRunOutcome,
    EnumWatchdogSelftest,
    ModelAutomationAlarmCleared,
    ModelAutomationAlarmRaised,
    ModelAutomationLivenessEntry,
    ModelAutomationLivenessVerdictEvent,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.watchdog_selftest import (
    Evaluator,
    default_planted_cases,
    run_selftest,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.watchdog_verdicts import (
    RawVerdict,
    evaluate_process,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    BUS_SILENCE_SECONDS,
    DIGEST_INTERVAL_SECONDS,
    HOURLY_CADENCE_SECONDS,
    ModelAutomationLivenessWatchdogRequest,
    ModelAutomationLivenessWatchdogResult,
    ModelProcessVerdict,
    ModelWatchdogEpisode,
    ModelWatchdogProcessState,
    ModelWatchdogState,
)

_V = EnumAutomationLivenessVerdict
_R = EnumAutomationLivenessReason

#: Episode ids are derived, never random: process, verdict and opening time.
EPISODE_NAMESPACE = uuid.UUID("6f1d3c52-8a8e-4f43-9d0e-2a6b1e0c2061")

#: Verdicts that open their episode without the confirmation hold.
_AT_ONCE: frozenset[EnumAutomationLivenessVerdict] = frozenset(
    {_V.FAILED, _V.UNDECLARED, _V.WATCHDOG_SELFTEST_FAILED}
)
_HEALTHY = RawVerdict(_V.HEALTHY, _R.INSIDE_BOUNDS)


def episode_id_for(
    process_id: str,
    host: str,
    verdict: EnumAutomationLivenessVerdict,
    opened_at: datetime,
) -> uuid.UUID:
    return uuid.uuid5(
        EPISODE_NAMESPACE,
        f"{process_id}|{host}|{verdict.value}|{opened_at.isoformat()}",
    )


def severity_of(raw: RawVerdict) -> EnumAlarmSeverity:
    """Low only for a single failed run; every other alarm is posted at once."""
    if raw.verdict is _V.FAILED and raw.reason in LOW_SEVERITY_REASONS:
        return EnumAlarmSeverity.LOW
    return EnumAlarmSeverity.HIGH


def delivery_due_at(
    severity: EnumAlarmSeverity, at: datetime, budget_seconds: int
) -> datetime:
    """High: now plus the budget. Low: the next 6-hourly digest plus the budget."""
    budget = timedelta(seconds=budget_seconds)
    if severity is EnumAlarmSeverity.HIGH:
        return at + budget
    epoch = datetime(1970, 1, 1, tzinfo=at.tzinfo)
    elapsed = int((at - epoch).total_seconds())
    next_digest = (elapsed // DIGEST_INTERVAL_SECONDS + 1) * DIGEST_INTERVAL_SECONDS
    return epoch + timedelta(seconds=next_digest) + budget


class _Subject:
    """One thing that gets a verdict this evaluation."""

    __slots__ = ("digest", "entry", "folded_into", "host", "process_id", "raw")

    def __init__(
        self,
        process_id: str,
        host: str,
        raw: RawVerdict,
        digest: str | None,
        entry: ModelAutomationLivenessEntry | None = None,
    ) -> None:
        self.process_id = process_id
        self.host = host
        self.raw = raw
        self.digest = digest
        self.entry = entry
        self.folded_into: EnumAutomationLivenessVerdict | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.process_id, self.host)


class HandlerAutomationLivenessWatchdog:
    """Evaluate one tick. No I/O, no clock, no network."""

    def __init__(self, evaluator: Evaluator = evaluate_process) -> None:
        self._evaluate = evaluator

    def handle(
        self, request: ModelAutomationLivenessWatchdogRequest
    ) -> ModelAutomationLivenessWatchdogResult:
        now = request.evaluated_at
        overlay_digest = request.overlay.digest()
        cases = (
            request.planted
            if request.planted is not None
            else default_planted_cases(now)
        )
        planted = run_selftest(cases, now, self._evaluate)
        selftest_ok = all(p.passed for p in planted)
        failed_names = ", ".join(p.name for p in planted if not p.passed)
        watchdog = _Subject(
            request.watchdog_process_id,
            request.watchdog_host,
            _HEALTHY
            if selftest_ok
            else RawVerdict(
                _V.WATCHDOG_SELFTEST_FAILED,
                _R.PLANTED_VERDICT_MISMATCH,
                f"planted contracts evaluated wrong: {failed_names}",
            ),
            overlay_digest,
        )

        if selftest_ok:
            subjects = self._subjects(request, overlay_digest)
            subjects[watchdog.key] = watchdog
        else:
            subjects = {watchdog.key: watchdog}

        prior = {p.key: p for p in request.state.processes}
        next_states: dict[tuple[str, str], ModelWatchdogProcessState] = {}
        if not selftest_ok:
            # Void: every real process keeps the state it had.
            next_states.update(prior)
        verdicts: list[ModelProcessVerdict] = []
        events: list[ModelAutomationLivenessVerdictEvent] = []
        raised: list[ModelAutomationAlarmRaised] = []
        cleared: list[ModelAutomationAlarmCleared] = []

        for key, subject in subjects.items():
            state, verdict_event, opened, closed = self._step(
                request, prior.get(key), subject
            )
            next_states[key] = state
            verdicts.append(
                ModelProcessVerdict(
                    process_id=subject.process_id,
                    host=subject.host,
                    verdict=subject.raw.verdict,
                    state=VERDICT_STATE[subject.raw.verdict],
                    reason=subject.raw.reason,
                    verdict_since=state.verdict_since,
                    contract_digest=subject.digest,
                    folded_into=subject.folded_into,
                    episode_id=state.episode.episode_id if state.episode else None,
                    detail=subject.raw.detail,
                )
            )
            if verdict_event is not None:
                events.append(verdict_event)
            raised.extend(opened)
            cleared.extend(closed)

        if selftest_ok:
            # Anything evaluated last time and gone now (retired, declared,
            # removed) passes: its open episode closes through the hold.
            for key, old in prior.items():
                if key in subjects or old.episode is None:
                    continue
                vanished = _Subject(old.process_id, old.host, _HEALTHY, None)
                state, _event, _opened, closed = self._step(request, old, vanished)
                cleared.extend(closed)
                if state.episode is not None:
                    next_states[key] = state

        return ModelAutomationLivenessWatchdogResult(
            evaluated_at=now,
            overlay_digest=overlay_digest,
            selftest=EnumWatchdogSelftest.PASS
            if selftest_ok
            else EnumWatchdogSelftest.FAIL,
            void=not selftest_ok,
            planted=planted,
            verdicts=tuple(sorted(verdicts, key=lambda v: (v.host, v.process_id))),
            verdict_events=tuple(events),
            alarms_raised=tuple(raised),
            alarms_cleared=tuple(cleared),
            state=ModelWatchdogState(
                evaluated_at=now,
                processes=tuple(next_states[k] for k in sorted(next_states)),
            ),
        )

    # ------------------------------------------------------------------ subjects

    def _subjects(
        self, request: ModelAutomationLivenessWatchdogRequest, overlay_digest: str
    ) -> dict[tuple[str, str], _Subject]:
        now = request.evaluated_at
        facts = {f.key: f for f in request.processes}
        subjects: dict[tuple[str, str], _Subject] = {}
        for entry in request.overlay.processes:
            if entry.state is EnumAutomationProcessState.RETIRED:
                continue
            key = (entry.process_id, entry.host)
            raw = self._evaluate(
                entry,
                facts.get(key),
                now,
                [e for e in request.expectations if (e.process_id, e.host) == key],
                external_deadman=entry.process_id
                == request.external_deadman_process_id,
            )
            subjects[key] = _Subject(
                entry.process_id, entry.host, raw, entry.digest(), entry
            )

        for undeclared in request.undeclared:
            key = (undeclared.process_id, undeclared.host)
            if key not in subjects:
                subjects[key] = _Subject(
                    undeclared.process_id,
                    undeclared.host,
                    RawVerdict(_V.UNDECLARED, _R.PROCESS_WITHOUT_ENTRY),
                    None,
                )

        silent_hosts: set[str] = set()
        for observer in request.observers:
            heard = observer.last_heartbeat_at or observer.expected_since
            if (now - heard).total_seconds() <= request.observer_silence:
                continue
            silent_hosts.add(observer.host)
            key = (observer.observer_process_id, observer.host)
            declared = subjects.get(key)
            subjects[key] = _Subject(
                observer.observer_process_id,
                observer.host,
                RawVerdict(
                    _V.HOST_SILENT,
                    _R.OBSERVER_HEARTBEAT_MISSED,
                    f"observer last heard {heard.isoformat()}",
                ),
                declared.digest if declared else overlay_digest,
                declared.entry if declared else None,
            )

        bus_silent = False
        bus = request.bus
        if bus is not None:
            heard_at = bus.last_event_at
            quiet = (
                heard_at is None
                or (now - heard_at).total_seconds() > BUS_SILENCE_SECONDS
            )
            if quiet and not bus.broker_answered:
                bus_silent = True
                key = (bus.bus_process_id, bus.host)
                declared = subjects.get(key)
                subjects[key] = _Subject(
                    bus.bus_process_id,
                    bus.host,
                    RawVerdict(
                        _V.BUS_SILENT,
                        _R.NO_EVENT_AND_BROKER_UNANSWERED,
                        "no event and the broker does not answer",
                    ),
                    declared.digest if declared else overlay_digest,
                    declared.entry if declared else None,
                )

        for subject in subjects.values():
            verdict = subject.raw.verdict
            if verdict in PASSING_VERDICTS or verdict is _V.BUS_SILENT:
                continue
            if bus_silent:
                subject.folded_into = _V.BUS_SILENT
            elif verdict is _V.MISSED and subject.host in silent_hosts:
                subject.folded_into = _V.HOST_SILENT
        return subjects

    # ------------------------------------------------------------------ episodes

    def _step(
        self,
        request: ModelAutomationLivenessWatchdogRequest,
        prev: ModelWatchdogProcessState | None,
        subject: _Subject,
    ) -> tuple[
        ModelWatchdogProcessState,
        ModelAutomationLivenessVerdictEvent | None,
        list[ModelAutomationAlarmRaised],
        list[ModelAutomationAlarmCleared],
    ]:
        now = request.evaluated_at
        timing = request.overlay.timing
        raw = subject.raw
        changed = prev is None or prev.verdict is not raw.verdict
        held = 1 if changed or prev is None else prev.held_evaluations + 1
        since = now if changed or prev is None else prev.verdict_since
        episode = prev.episode if prev is not None else None
        passing = prev.passing_evaluations if prev is not None else 0
        opened: list[ModelAutomationAlarmRaised] = []
        closed: list[ModelAutomationAlarmCleared] = []

        verdict_event = None
        if changed and (subject.digest is not None or raw.verdict is _V.UNDECLARED):
            verdict_event = ModelAutomationLivenessVerdictEvent(
                process_id=subject.process_id,
                host=subject.host,
                verdict=raw.verdict,
                state=VERDICT_STATE[raw.verdict],
                reason=raw.reason,
                previous_verdict=prev.verdict if prev is not None else None,
                verdict_since=since,
                evaluated_at=now,
                contract_digest=None
                if raw.verdict is _V.UNDECLARED
                else subject.digest,
                detail=raw.detail,
            )

        if raw.verdict in PASSING_VERDICTS:
            if episode is not None:
                passing += 1
                if passing >= self._close_after(request, subject, episode):
                    closed.append(_cleared(episode, now))
                    episode = None
                    passing = 0
            else:
                passing = 0
        elif subject.folded_into is None:
            passing = 0
            severity = severity_of(raw)
            if episode is not None and episode.verdict is raw.verdict:
                rise = (
                    episode.severity is EnumAlarmSeverity.LOW
                    and severity is EnumAlarmSeverity.HIGH
                )
                episode = episode.model_copy(
                    update={
                        "reason": raw.reason,
                        "severity": EnumAlarmSeverity.HIGH
                        if rise
                        else episode.severity,
                        "delivery_due_at": delivery_due_at(
                            severity, now, timing.delivery_budget_seconds
                        )
                        if rise
                        else episode.delivery_due_at,
                    }
                )
                if rise:
                    opened.append(_raised(episode))
            elif raw.verdict in _AT_ONCE or held >= timing.confirmation_evaluations:
                if episode is not None:
                    # A different failing verdict supersedes the open episode.
                    closed.append(_cleared(episode, now))
                episode = ModelWatchdogEpisode(
                    episode_id=episode_id_for(
                        subject.process_id, subject.host, raw.verdict, now
                    ),
                    process_id=subject.process_id,
                    host=subject.host,
                    verdict=raw.verdict,
                    reason=raw.reason,
                    severity=severity,
                    opened_at=now,
                    delivery_due_at=delivery_due_at(
                        severity, now, timing.delivery_budget_seconds
                    ),
                )
                opened.append(_raised(episode))

        state = ModelWatchdogProcessState(
            process_id=subject.process_id,
            host=subject.host,
            verdict=raw.verdict,
            reason=raw.reason,
            verdict_since=since,
            held_evaluations=held,
            passing_evaluations=passing,
            episode=episode,
        )
        return state, verdict_event, opened, closed

    def _close_after(
        self,
        request: ModelAutomationLivenessWatchdogRequest,
        subject: _Subject,
        episode: ModelWatchdogEpisode,
    ) -> int:
        """One healthy run closes a slow process's episode; otherwise the hold."""
        hold = request.overlay.timing.confirmation_evaluations
        entry = subject.entry
        if entry is None or entry.expected_interval_seconds is None:
            return hold
        if entry.expected_interval_seconds < HOURLY_CADENCE_SECONDS:
            return hold
        facts = next(
            (f for f in request.processes if f.key == subject.key),
            None,
        )
        newest = facts.newest_run if facts is not None else None
        if (
            newest is not None
            and newest.outcome is EnumAutomationRunOutcome.OK
            and newest.finished_at >= episode.opened_at
        ):
            return 1
        return hold


def _raised(episode: ModelWatchdogEpisode) -> ModelAutomationAlarmRaised:
    action = "declare" if episode.verdict is _V.UNDECLARED else "diagnose"
    return ModelAutomationAlarmRaised(
        episode_id=episode.episode_id,
        process_id=episode.process_id,
        host=episode.host,
        verdict=episode.verdict,
        state=VERDICT_STATE[episode.verdict],
        reason=episode.reason,
        severity=episode.severity,
        opened_at=episode.opened_at,
        delivery_due_at=episode.delivery_due_at,
        evidence_ref=f"watchdog:{episode.process_id}@{episode.host}",
        action=f"{action}:{episode.process_id}",
    )


def _cleared(
    episode: ModelWatchdogEpisode, now: datetime
) -> ModelAutomationAlarmCleared:
    return ModelAutomationAlarmCleared(
        episode_id=episode.episode_id,
        process_id=episode.process_id,
        host=episode.host,
        verdict=episode.verdict,
        opened_at=episode.opened_at,
        cleared_at=now,
    )


__all__ = [
    "EPISODE_NAMESPACE",
    "HandlerAutomationLivenessWatchdog",
    "delivery_due_at",
    "episode_id_for",
    "severity_of",
]
