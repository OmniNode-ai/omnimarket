# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and carried state of the automation-liveness watchdog compute.

The compute is pure. The effect that ticks it (a later node) folds the bus
events into one ``ModelProcessRunFacts`` per declared process, passes the
watchdog state it persisted after the previous evaluation, and gets back the
verdicts, the seam events to publish and the state to persist next. Nothing
here reads a clock: ``evaluated_at`` is the only "now".

The per-process facts are shaped so the liveness projection's state row and run
history map onto them field for field; ``adapter_projection_rows`` is that
mapping.
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from omnibase_core.enums.enum_liveness_state import EnumLivenessState
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    model_validator,
)

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAlarmSeverity,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationRunOutcome,
    EnumWatchdogSelftest,
    ModelAutomationAlarmCleared,
    ModelAutomationAlarmRaised,
    ModelAutomationLivenessEntry,
    ModelAutomationLivenessOverlay,
    ModelAutomationLivenessVerdictEvent,
    ModelTriggerExpectation,
    NonEmptyStr,
    ProcessId,
    Sha256Hex,
)

#: The external dead-man raises after this long with no completed run (4.8).
EXTERNAL_DEADMAN_SILENCE_SECONDS = 2 * 60 * 60
#: No event from any host for this long, with the broker silent, is BUS_SILENT.
BUS_SILENCE_SECONDS = 10 * 60
#: A ``not_installed`` entry alarms after this grace.
NOT_INSTALLED_GRACE_SECONDS = 24 * 60 * 60
#: Low-severity episodes are due in the next digest of this period.
DIGEST_INTERVAL_SECONDS = 6 * 60 * 60
#: Runs the failure window looks back over.
FAILURE_WINDOW_RUNS = 6
#: Failures inside the window that make it a window (fast cadences).
FAILURE_WINDOW_THRESHOLD = 2
#: A cadence this slow or slower treats any failure as a window.
SLOW_CADENCE_SECONDS = 6 * 60 * 60
#: A cadence this slow or slower closes its episode on one healthy run.
HOURLY_CADENCE_SECONDS = 60 * 60


# --------------------------------------------------------------------------- facts


class ModelRunFact(BaseModel):
    """One finished run, as the fold keeps it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: NonEmptyStr
    started_at: AwareDatetime
    finished_at: AwareDatetime
    outcome: EnumAutomationRunOutcome
    did_work_count: NonNegativeInt
    #: Null when the entry declares ``demand: none`` or the read failed.
    demand_count: NonNegativeInt | None = None
    unseen_runs: NonNegativeInt = 0


class ModelObserverReport(BaseModel):
    """A verdict an observer published for the process itself.

    Observers report what only they can see: a disabled schedule (MISSED,
    ``workflow_disabled``) and an unreadable source (UNOBSERVABLE).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumAutomationLivenessVerdict
    reason: EnumAutomationLivenessReason
    reported_at: AwareDatetime

    @model_validator(mode="after")
    def _observer_verdicts_only(self) -> Self:
        if self.reason not in OBSERVER_REASONS:
            raise ValueError(
                f"{self.reason.value} is not a verdict an observer reports"
            )
        if OBSERVER_REASONS[self.reason] is not self.verdict:
            raise ValueError(
                f"reason {self.reason.value} does not belong to {self.verdict.value}"
            )
        return self


_R = EnumAutomationLivenessReason
_V = EnumAutomationLivenessVerdict
#: Reasons an observer publishes for a declared process, with their verdict.
OBSERVER_REASONS: dict[EnumAutomationLivenessReason, EnumAutomationLivenessVerdict] = {
    _R.WORKFLOW_DISABLED: _V.MISSED,
    _R.EVIDENCE_UNREADABLE: _V.UNOBSERVABLE,
    _R.DEMAND_UNREADABLE: _V.UNOBSERVABLE,
    _R.UNSEEN_RUNS: _V.UNOBSERVABLE,
    _R.SOURCE_STALE: _V.UNOBSERVABLE,
    _R.READ_INCOMPLETE: _V.UNOBSERVABLE,
}
del _R, _V


class ModelProcessRunFacts(BaseModel):
    """What the fold knows about one declared process on one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    host: NonEmptyStr
    #: When the overlay that declares the process was loaded.
    declared_at: AwareDatetime | None = None
    #: Finished runs, oldest first; the newest few are enough.
    runs: tuple[ModelRunFact, ...] = ()
    #: Start of the oldest run with no finish.
    open_run_started_at: AwareDatetime | None = None
    #: The newest start or finish of any run.
    last_run_at: AwareDatetime | None = None
    last_heartbeat_at: AwareDatetime | None = None
    last_progress_at: AwareDatetime | None = None
    progress_counter: NonNegativeInt | None = None
    #: Demand the daemon's newest heartbeat carried.
    heartbeat_demand_count: NonNegativeInt | None = None
    observer_report: ModelObserverReport | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.process_id, self.host)

    @property
    def newest_run(self) -> ModelRunFact | None:
        return self.runs[-1] if self.runs else None


class ModelObserverFacts(BaseModel):
    """One host observer's heartbeat, for HOST_SILENT."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: NonEmptyStr
    observer_process_id: ProcessId
    #: When the observer started being expected; ages a never-seen observer.
    expected_since: AwareDatetime
    last_heartbeat_at: AwareDatetime | None = None


class ModelBusFacts(BaseModel):
    """What the leg sees of the bus, for BUS_SILENT."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bus_process_id: ProcessId
    host: NonEmptyStr
    #: The newest event from any host.
    last_event_at: AwareDatetime | None = None
    broker_answered: bool


class ModelUndeclaredProcess(BaseModel):
    """A process of ours an observer saw running with no overlay entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    host: NonEmptyStr
    reported_at: AwareDatetime


class ModelPlantedCase(BaseModel):
    """A planted contract with known facts and the verdict it must get."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonEmptyStr
    entry: ModelAutomationLivenessEntry
    facts: ModelProcessRunFacts
    expected_verdict: EnumAutomationLivenessVerdict
    expected_reason: EnumAutomationLivenessReason

    @model_validator(mode="after")
    def _facts_match_entry(self) -> Self:
        if self.facts.key != (self.entry.process_id, self.entry.host):
            raise ValueError(f"{self.name}: facts and entry name different processes")
        return self


# --------------------------------------------------------------------------- state


class ModelWatchdogEpisode(BaseModel):
    """One open alarm episode, as the watchdog carries it between evaluations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: UUID
    process_id: ProcessId
    host: NonEmptyStr
    verdict: EnumAutomationLivenessVerdict
    reason: EnumAutomationLivenessReason
    severity: EnumAlarmSeverity
    opened_at: AwareDatetime
    delivery_due_at: AwareDatetime
    detail: str = ""


class ModelWatchdogProcessState(BaseModel):
    """The watchdog's memory of one process (or host, bus or monitor leg)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    host: NonEmptyStr
    verdict: EnumAutomationLivenessVerdict
    reason: EnumAutomationLivenessReason
    verdict_since: AwareDatetime
    #: Consecutive evaluations the current verdict has held.
    held_evaluations: PositiveInt = 1
    #: Consecutive passing evaluations while an episode is open.
    passing_evaluations: NonNegativeInt = 0
    episode: ModelWatchdogEpisode | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.process_id, self.host)


class ModelWatchdogState(BaseModel):
    """Everything the watchdog carries from one evaluation to the next."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_at: AwareDatetime | None = None
    processes: tuple[ModelWatchdogProcessState, ...] = ()

    def get(self, process_id: str, host: str) -> ModelWatchdogProcessState | None:
        return next(
            (p for p in self.processes if p.key == (process_id, host)),
            None,
        )


# --------------------------------------------------------------------------- request


class ModelAutomationLivenessWatchdogRequest(BaseModel):
    """One evaluation: the overlay, the folded facts, the carried state, the time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_at: AwareDatetime
    overlay: ModelAutomationLivenessOverlay
    #: The leg evaluating; its self-test verdict is reported under this id.
    watchdog_process_id: ProcessId
    watchdog_host: NonEmptyStr
    processes: tuple[ModelProcessRunFacts, ...] = ()
    observers: tuple[ModelObserverFacts, ...] = ()
    bus: ModelBusFacts | None = None
    expectations: tuple[ModelTriggerExpectation, ...] = ()
    undeclared: tuple[ModelUndeclaredProcess, ...] = ()
    #: The overlay entry of the external dead-man, when the leg watches it.
    external_deadman_process_id: ProcessId | None = None
    #: An observer heartbeat older than this is HOST_SILENT; default three polls.
    observer_silence_seconds: PositiveInt | None = None
    #: Planted contracts; None evaluates the built-in set.
    planted: tuple[ModelPlantedCase, ...] | None = None
    state: ModelWatchdogState = Field(default_factory=ModelWatchdogState)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        declared = {(e.process_id, e.host) for e in self.overlay.processes}
        seen: set[tuple[str, str]] = set()
        for facts in self.processes:
            if facts.key not in declared:
                raise ValueError(
                    f"facts for {facts.process_id}@{facts.host} have no overlay entry; "
                    "an undeclared process is reported in `undeclared`"
                )
            if facts.key in seen:
                raise ValueError(f"duplicate facts for {facts.process_id}@{facts.host}")
            seen.add(facts.key)
        if self.external_deadman_process_id is not None and not any(
            e.process_id == self.external_deadman_process_id
            for e in self.overlay.processes
        ):
            raise ValueError(
                f"external dead-man {self.external_deadman_process_id} has no overlay entry"
            )
        if (
            self.state.evaluated_at is not None
            and self.state.evaluated_at > self.evaluated_at
        ):
            raise ValueError("the carried state is newer than this evaluation")
        return self

    @property
    def observer_silence(self) -> int:
        if self.observer_silence_seconds is not None:
            return self.observer_silence_seconds
        return 3 * self.overlay.timing.observer_interval_seconds


# --------------------------------------------------------------------------- result


class ModelProcessVerdict(BaseModel):
    """The verdict of one process at this evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    host: NonEmptyStr
    verdict: EnumAutomationLivenessVerdict
    state: EnumLivenessState
    reason: EnumAutomationLivenessReason
    verdict_since: AwareDatetime
    contract_digest: Sha256Hex | None = None
    #: The monitor verdict this one folds into (HOST_SILENT or BUS_SILENT);
    #: a folded verdict opens no episode of its own.
    folded_into: EnumAutomationLivenessVerdict | None = None
    #: The open episode, if any.
    episode_id: UUID | None = None
    detail: str = ""


class ModelPlantedOutcome(BaseModel):
    """One planted contract's expected and actual verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonEmptyStr
    expected_verdict: EnumAutomationLivenessVerdict
    expected_reason: EnumAutomationLivenessReason
    actual_verdict: EnumAutomationLivenessVerdict
    actual_reason: EnumAutomationLivenessReason

    @property
    def passed(self) -> bool:
        return (
            self.expected_verdict is self.actual_verdict
            and self.expected_reason is self.actual_reason
        )


class ModelAutomationLivenessWatchdogResult(BaseModel):
    """The verdicts, the seam events to publish and the state to persist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_at: AwareDatetime
    overlay_digest: Sha256Hex
    selftest: EnumWatchdogSelftest
    #: True when a planted contract evaluated wrong: no real verdict, episode
    #: or state change came out of this evaluation, only the self-test alarm.
    void: bool
    planted: tuple[ModelPlantedOutcome, ...]
    verdicts: tuple[ModelProcessVerdict, ...]
    verdict_events: tuple[ModelAutomationLivenessVerdictEvent, ...]
    #: Opened episodes, and a severity rise of an open one (same episode_id).
    alarms_raised: tuple[ModelAutomationAlarmRaised, ...]
    alarms_cleared: tuple[ModelAutomationAlarmCleared, ...]
    state: ModelWatchdogState


__all__ = [
    "BUS_SILENCE_SECONDS",
    "DIGEST_INTERVAL_SECONDS",
    "EXTERNAL_DEADMAN_SILENCE_SECONDS",
    "FAILURE_WINDOW_RUNS",
    "FAILURE_WINDOW_THRESHOLD",
    "HOURLY_CADENCE_SECONDS",
    "NOT_INSTALLED_GRACE_SECONDS",
    "OBSERVER_REASONS",
    "SLOW_CADENCE_SECONDS",
    "ModelAutomationLivenessWatchdogRequest",
    "ModelAutomationLivenessWatchdogResult",
    "ModelBusFacts",
    "ModelObserverFacts",
    "ModelObserverReport",
    "ModelPlantedCase",
    "ModelPlantedOutcome",
    "ModelProcessRunFacts",
    "ModelProcessVerdict",
    "ModelRunFact",
    "ModelTriggerExpectation",
    "ModelUndeclaredProcess",
    "ModelWatchdogEpisode",
    "ModelWatchdogProcessState",
    "ModelWatchdogState",
]
