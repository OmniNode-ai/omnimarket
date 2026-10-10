# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Automation-liveness contract entry, overlay and bus events (OMN-20793).

Every automatic process declares one entry in the liveness overlay: what a
healthy run looks like, where its run facts are read, what counts as work and
as waiting work, and how fast its failure must reach someone. The overlay ships
here as a schema and a neutral default that declares nothing; the processes we
run are deployment facts and live in a private overlay.

**Every bound is derived from the alarm deadline.** The overlay refuses an
entry when the worst case of any verdict it can reach -- MISSED, FAILED,
OVERRUN, IDLE_WITH_DEMAND, TRIGGER_UNANSWERED, UNOBSERVABLE -- plus the
observer interval, the confirmation hold, one outbox retry and the delivery
budget, exceeds ``alarm_deadline_seconds``. An entry that cannot raise in time
is refused at load, not discovered during an outage.

**A run the observer did not see is not a healthy run.** ``latest-only``
evidence is accepted only for a process that runs no more often than every
third observer poll, and a cron entry must name its
completion record: a start record alone proves only a start.

**There is no mute switch.** A process meant to stop is retired by changing its
entry's ``state``; nothing here silences an entry.

The verdict vocabulary maps onto ``EnumLivenessState`` from omnibase_core. The
runtime-shaped ``ModelLivenessReceipt`` is not reused: it requires a deployed
sha and an image digest that a launchd job or a scheduled workflow does not
have.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from functools import cache
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self
from uuid import UUID

import yaml
from omnibase_core.enums.enum_liveness_state import EnumLivenessState
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)

NonEmptyStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"\S")
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ProcessId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, pattern=r"^[a-z0-9][a-z0-9._/-]*$"
    ),
]

OVERLAY_SCHEMA_VERSION = "automation-liveness-overlay/v1"
#: The ``demand`` value of a process whose every run is work.
DEMAND_NONE = "none"

_PACKAGE = "omnimarket.models.liveness"
_TOPIC_CONTRACT_FILE = "automation_liveness.contract.yaml"
_DEFAULT_OVERLAY_FILE = "automation_liveness_overlay.default.yaml"


# --------------------------------------------------------------------------- enums


class EnumAutomationTriggerKind(StrEnum):
    """What starts a process; the native id names the instance."""

    LAUNCHD_CALENDAR = "launchd-calendar"
    LAUNCHD_INTERVAL = "launchd-interval"
    LAUNCHD_KEEPALIVE = "launchd-keepalive"
    SYSTEMD_TIMER = "systemd-timer"
    SYSTEMD_SERVICE = "systemd-service"
    CRON = "cron"
    CONTAINER = "container"
    BUS_CONSUMER = "bus-consumer"
    GH_SCHEDULE = "gh-schedule"
    GH_EVENT = "gh-event"
    SESSION_CRON = "session-cron"
    ROUTINE = "routine"


#: Long-running kinds: progress, not discrete runs, is what they report.
DAEMON_TRIGGER_KINDS: frozenset[EnumAutomationTriggerKind] = frozenset(
    {
        EnumAutomationTriggerKind.LAUNCHD_KEEPALIVE,
        EnumAutomationTriggerKind.SYSTEMD_SERVICE,
        EnumAutomationTriggerKind.CONTAINER,
        EnumAutomationTriggerKind.BUS_CONSUMER,
    }
)

#: Kinds with no cadence of their own; a correlation states their deadline.
EVENT_TRIGGER_KINDS: frozenset[EnumAutomationTriggerKind] = frozenset(
    {EnumAutomationTriggerKind.GH_EVENT}
)

#: GitHub delays and drops scheduled runs under load (90 minutes observed).
GH_SCHEDULE_DELAY_SECONDS = 90 * 60
#: Session crons and Routines jitter by up to 15 minutes.
SESSION_JITTER_SECONDS = 15 * 60


def trigger_kind_slack_seconds(
    kind: EnumAutomationTriggerKind, interval_seconds: int
) -> int:
    """Slack added to two intervals for the default ``max_silence_seconds``.

    A GitHub schedule gets one more interval plus 90 minutes, because GitHub
    delays and drops scheduled runs; a session cron or Routine gets its jitter;
    a host scheduler gets none.
    """
    if kind is EnumAutomationTriggerKind.GH_SCHEDULE:
        return interval_seconds + GH_SCHEDULE_DELAY_SECONDS
    if kind in (
        EnumAutomationTriggerKind.SESSION_CRON,
        EnumAutomationTriggerKind.ROUTINE,
    ):
        return SESSION_JITTER_SECONDS
    return 0


class EnumAutomationEmitter(StrEnum):
    """Who reports a process's runs on the bus."""

    #: The process publishes its own run and heartbeat events.
    SELF = "self"
    #: A host or GitHub observer reads the declared evidence and reports for it.
    OBSERVER = "observer"


class EnumAutomationRunRecord(StrEnum):
    """How much run history the evidence keeps."""

    #: Every run's start and completion are recoverable.
    PER_RUN = "per-run"
    #: Only the newest run's outcome is readable.
    LATEST_ONLY = "latest-only"


class EnumAutomationEvidenceSource(StrEnum):
    """Where an observer reads a process's run facts."""

    LAUNCHD_STATE = "launchd_state"
    SYSTEMD_JOURNAL = "systemd_journal"
    RECEIPTS_FILE = "receipts_file"
    STATE_FILE = "state_file"
    LOG_LINE = "log_line"
    LEDGER_ROWS = "ledger_rows"
    CONSUMER_GROUP = "consumer_group"
    WORKFLOW_RUNS = "workflow_runs"
    HEARTBEAT_FILE = "heartbeat_file"
    PROBE = "probe"


class EnumAutomationProcessState(StrEnum):
    """Lifecycle of a declared process."""

    ACTIVE = "active"
    #: Declared but not installed yet; alarms after the grace period.
    NOT_INSTALLED = "not_installed"
    #: Meant to be gone; the completeness gate holds it uninstalled.
    RETIRED = "retired"


class EnumPositiveControlKind(StrEnum):
    """What proves an entry raises when its process fails."""

    REPLAY_FIXTURE = "replay_fixture"
    CLASS_DRILL = "class_drill"


class EnumAutomationRunPhase(StrEnum):
    STARTED = "started"
    FINISHED = "finished"


class EnumAutomationRunOutcome(StrEnum):
    """How a finished run ended."""

    OK = "ok"
    FAILED = "failed"
    DEGRADED = "degraded"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


#: Outcomes that open or extend a FAILED episode.
FAILING_OUTCOMES: frozenset[EnumAutomationRunOutcome] = frozenset(
    {
        EnumAutomationRunOutcome.FAILED,
        EnumAutomationRunOutcome.DEGRADED,
        EnumAutomationRunOutcome.REFUSED,
        EnumAutomationRunOutcome.TIMEOUT,
    }
)


class EnumAutomationLivenessVerdict(StrEnum):
    """The watchdog's verdict for one process (or host, bus or monitor leg)."""

    HEALTHY = "healthy"
    NO_DEMAND = "no_demand"
    NOT_READY = "not_ready"
    MISSED = "missed"
    FAILED = "failed"
    OVERRUN = "overrun"
    IDLE_WITH_DEMAND = "idle_with_demand"
    UNOBSERVABLE = "unobservable"
    TRIGGER_UNANSWERED = "trigger_unanswered"
    UNDECLARED = "undeclared"
    HOST_SILENT = "host_silent"
    BUS_SILENT = "bus_silent"
    #: Seen by the dead-man: the primary watchdog's heartbeat is missing.
    PRIMARY_SILENT = "primary_silent"
    #: Seen by the primary: the dead-man is not consuming, recording or beating.
    DEADMAN_STALLED = "deadman_stalled"
    #: The daily canary episode was not delivered or not recorded in time.
    ALARM_PATH_BROKEN = "alarm_path_broken"
    #: The external dead-man has no completed run in its window.
    EXTERNAL_DEADMAN_SILENT = "external_deadman_silent"
    #: A planted contract's verdict differed from its planted answer.
    WATCHDOG_SELFTEST_FAILED = "watchdog_selftest_failed"


VERDICT_STATE: Mapping[EnumAutomationLivenessVerdict, EnumLivenessState] = {
    EnumAutomationLivenessVerdict.HEALTHY: EnumLivenessState.HEALTHY,
    EnumAutomationLivenessVerdict.NO_DEMAND: EnumLivenessState.NO_DEMAND,
    EnumAutomationLivenessVerdict.NOT_READY: EnumLivenessState.NOT_READY,
    EnumAutomationLivenessVerdict.MISSED: EnumLivenessState.STALE,
    EnumAutomationLivenessVerdict.FAILED: EnumLivenessState.RED,
    EnumAutomationLivenessVerdict.OVERRUN: EnumLivenessState.RED,
    EnumAutomationLivenessVerdict.IDLE_WITH_DEMAND: EnumLivenessState.RED,
    EnumAutomationLivenessVerdict.UNOBSERVABLE: EnumLivenessState.RED,
    EnumAutomationLivenessVerdict.TRIGGER_UNANSWERED: EnumLivenessState.RED,
    EnumAutomationLivenessVerdict.UNDECLARED: EnumLivenessState.RED,
    EnumAutomationLivenessVerdict.HOST_SILENT: EnumLivenessState.STALE,
    EnumAutomationLivenessVerdict.BUS_SILENT: EnumLivenessState.STALE,
    EnumAutomationLivenessVerdict.PRIMARY_SILENT: EnumLivenessState.STALE,
    EnumAutomationLivenessVerdict.DEADMAN_STALLED: EnumLivenessState.RED,
    EnumAutomationLivenessVerdict.ALARM_PATH_BROKEN: EnumLivenessState.RED,
    EnumAutomationLivenessVerdict.EXTERNAL_DEADMAN_SILENT: EnumLivenessState.STALE,
    EnumAutomationLivenessVerdict.WATCHDOG_SELFTEST_FAILED: EnumLivenessState.RED,
}

#: Verdicts that never open an alarm episode.
PASSING_VERDICTS: frozenset[EnumAutomationLivenessVerdict] = frozenset(
    {
        EnumAutomationLivenessVerdict.HEALTHY,
        EnumAutomationLivenessVerdict.NO_DEMAND,
        EnumAutomationLivenessVerdict.NOT_READY,
    }
)


class EnumAutomationLivenessReason(StrEnum):
    """Why a verdict holds. Each reason belongs to exactly one verdict."""

    INSIDE_BOUNDS = "inside_bounds"
    DEMAND_READ_ZERO = "demand_read_zero"
    NOT_INSTALLED_IN_GRACE = "not_installed_in_grace"
    NO_RUN_IN_MAX_SILENCE = "no_run_in_max_silence"
    WORKFLOW_DISABLED = "workflow_disabled"
    NOT_INSTALLED_PAST_GRACE = "not_installed_past_grace"
    RUN_FAILED = "run_failed"
    RUN_DEGRADED = "run_degraded"
    RUN_REFUSED = "run_refused"
    RUN_TIMEOUT = "run_timeout"
    FAILURE_WINDOW = "failure_window"
    RUN_OPEN_PAST_MAX_RUNTIME = "run_open_past_max_runtime"
    PROGRESS_STALLED_WITH_DEMAND = "progress_stalled_with_demand"
    CONSECUTIVE_IDLE_RUNS_WITH_DEMAND = "consecutive_idle_runs_with_demand"
    EVIDENCE_UNREADABLE = "evidence_unreadable"
    DEMAND_UNREADABLE = "demand_unreadable"
    UNSEEN_RUNS = "unseen_runs"
    SOURCE_STALE = "source_stale"
    READ_INCOMPLETE = "read_incomplete"
    ANSWER_PAST_DEADLINE = "answer_past_deadline"
    PROCESS_WITHOUT_ENTRY = "process_without_entry"
    OBSERVER_HEARTBEAT_MISSED = "observer_heartbeat_missed"
    NO_EVENT_AND_BROKER_UNANSWERED = "no_event_and_broker_unanswered"
    PRIMARY_HEARTBEAT_MISSED = "primary_heartbeat_missed"
    DEADMAN_HEARTBEAT_MISSED = "deadman_heartbeat_missed"
    CONSUMED_OFFSET_STALLED = "consumed_offset_stalled"
    OUTBOX_BACKLOG_GROWING = "outbox_backlog_growing"
    LEDGER_WRITE_OVERDUE = "ledger_write_overdue"
    CANARY_UNDELIVERED = "canary_undelivered"
    CANARY_UNRECORDED = "canary_unrecorded"
    EXTERNAL_RUN_MISSING = "external_run_missing"
    PLANTED_VERDICT_MISMATCH = "planted_verdict_mismatch"


_V = EnumAutomationLivenessVerdict
_R = EnumAutomationLivenessReason
VERDICT_REASONS: Mapping[
    EnumAutomationLivenessVerdict, frozenset[EnumAutomationLivenessReason]
] = {
    _V.HEALTHY: frozenset({_R.INSIDE_BOUNDS}),
    _V.NO_DEMAND: frozenset({_R.DEMAND_READ_ZERO}),
    _V.NOT_READY: frozenset({_R.NOT_INSTALLED_IN_GRACE}),
    _V.MISSED: frozenset(
        {_R.NO_RUN_IN_MAX_SILENCE, _R.WORKFLOW_DISABLED, _R.NOT_INSTALLED_PAST_GRACE}
    ),
    _V.FAILED: frozenset(
        {
            _R.RUN_FAILED,
            _R.RUN_DEGRADED,
            _R.RUN_REFUSED,
            _R.RUN_TIMEOUT,
            _R.FAILURE_WINDOW,
        }
    ),
    _V.OVERRUN: frozenset(
        {_R.RUN_OPEN_PAST_MAX_RUNTIME, _R.PROGRESS_STALLED_WITH_DEMAND}
    ),
    _V.IDLE_WITH_DEMAND: frozenset({_R.CONSECUTIVE_IDLE_RUNS_WITH_DEMAND}),
    _V.UNOBSERVABLE: frozenset(
        {
            _R.EVIDENCE_UNREADABLE,
            _R.DEMAND_UNREADABLE,
            _R.UNSEEN_RUNS,
            _R.SOURCE_STALE,
            _R.READ_INCOMPLETE,
        }
    ),
    _V.TRIGGER_UNANSWERED: frozenset({_R.ANSWER_PAST_DEADLINE}),
    _V.UNDECLARED: frozenset({_R.PROCESS_WITHOUT_ENTRY}),
    _V.HOST_SILENT: frozenset({_R.OBSERVER_HEARTBEAT_MISSED}),
    _V.BUS_SILENT: frozenset({_R.NO_EVENT_AND_BROKER_UNANSWERED}),
    _V.PRIMARY_SILENT: frozenset({_R.PRIMARY_HEARTBEAT_MISSED}),
    _V.DEADMAN_STALLED: frozenset(
        {
            _R.DEADMAN_HEARTBEAT_MISSED,
            _R.CONSUMED_OFFSET_STALLED,
            _R.OUTBOX_BACKLOG_GROWING,
            _R.LEDGER_WRITE_OVERDUE,
        }
    ),
    _V.ALARM_PATH_BROKEN: frozenset({_R.CANARY_UNDELIVERED, _R.CANARY_UNRECORDED}),
    _V.EXTERNAL_DEADMAN_SILENT: frozenset({_R.EXTERNAL_RUN_MISSING}),
    _V.WATCHDOG_SELFTEST_FAILED: frozenset({_R.PLANTED_VERDICT_MISMATCH}),
}
del _V, _R

#: The only reasons that may carry a low-severity episode: one failed run.
LOW_SEVERITY_REASONS: frozenset[EnumAutomationLivenessReason] = frozenset(
    {
        EnumAutomationLivenessReason.RUN_FAILED,
        EnumAutomationLivenessReason.RUN_DEGRADED,
        EnumAutomationLivenessReason.RUN_REFUSED,
        EnumAutomationLivenessReason.RUN_TIMEOUT,
    }
)


class EnumAlarmSeverity(StrEnum):
    #: Recorded at once, posted in the next digest.
    LOW = "low"
    #: Recorded and posted at once.
    HIGH = "high"


class EnumAlarmDeliveryRoute(StrEnum):
    #: The alert channel; the receipt is the Slack message timestamp.
    SLACK = "slack"
    #: The external dead-man's issue; the receipt is the issue reference.
    GITHUB_ISSUE = "github_issue"


class EnumWatchdogSelftest(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class EnumAutomationLivenessEvent(StrEnum):
    """The events of the seam; each has one topic in the topic contract."""

    RUN_OBSERVED = "run_observed"
    HEARTBEAT = "heartbeat"
    LIVENESS_DECLARED = "liveness_declared"
    LIVENESS_VERDICT = "liveness_verdict"
    ALARM_RAISED = "alarm_raised"
    ALARM_CLEARED = "alarm_cleared"
    ALARM_DELIVERED = "alarm_delivered"
    ALARM_RECORDED = "alarm_recorded"


def _canonical_digest(model: BaseModel) -> str:
    payload = json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- overlay


class ModelAutomationTrigger(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumAutomationTriggerKind
    #: Launchd label, systemd unit, cron file and line, container, consumer
    #: group or workflow file.
    native_id: NonEmptyStr


class ModelAutomationLivenessEvidence(BaseModel):
    """Where the run facts are read: required for an observer entry and a cron entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: EnumAutomationEvidenceSource
    #: The file, label, unit, group, lane or command the source is read at.
    locator: NonEmptyStr
    #: The field or pattern that marks a run's completion with its exit. A
    #: cron entry must name one: a start record alone proves only a start.
    completion_record: NonEmptyStr | None = None


class ModelAutomationCorrelation(BaseModel):
    """Trigger and answer of an event-triggered process."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_event: NonEmptyStr
    answer_event: NonEmptyStr
    match_key: NonEmptyStr
    answer_deadline_seconds: PositiveInt


class ModelAutomationPositiveControl(BaseModel):
    """The replay fixture or class drill that proves the entry raises."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumPositiveControlKind
    ref: NonEmptyStr


class ModelAutomationLivenessTiming(BaseModel):
    """The monitor's own latencies, added to every verdict's bound.

    Each default is also the floor: an overlay may declare a slower monitor,
    which only widens every bound, but never a faster one than the design runs,
    which would let the deadline arithmetic pass on latencies nobody delivers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observer_interval_seconds: int = Field(default=60, ge=60)
    evaluation_interval_seconds: int = Field(default=60, ge=60)
    #: Evaluations a verdict must hold before its episode opens.
    confirmation_evaluations: int = Field(default=2, ge=2)
    outbox_retry_seconds: int = Field(default=120, ge=120)
    delivery_budget_seconds: int = Field(default=120, ge=120)

    @property
    def overhead_seconds(self) -> int:
        """Observation, confirmation hold, one outbox retry and delivery."""
        return (
            self.observer_interval_seconds
            + self.confirmation_evaluations * self.evaluation_interval_seconds
            + self.outbox_retry_seconds
            + self.delivery_budget_seconds
        )


class ModelAutomationLivenessEntry(BaseModel):
    """One automatic process's liveness contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    owner_repo: NonEmptyStr
    host: NonEmptyStr
    trigger: ModelAutomationTrigger
    #: The cadence; for a daemon, the progress interval. Absent only for an
    #: event-triggered kind, whose correlation carries the deadline.
    expected_interval_seconds: PositiveInt | None = None
    alarm_deadline_seconds: PositiveInt
    #: No run or progress for this long is MISSED. Defaults to two intervals
    #: plus the trigger kind's slack (``trigger_kind_slack_seconds``).
    max_silence_seconds: PositiveInt | None = None
    #: A run open past this is OVERRUN. Required for kinds with discrete runs.
    max_runtime_seconds: PositiveInt | None = None
    emitter: EnumAutomationEmitter
    run_record: EnumAutomationRunRecord
    evidence: ModelAutomationLivenessEvidence | None = None
    #: The field or pattern that counts work done in a run.
    real_work: NonEmptyStr
    #: How to tell work was waiting, or ``none`` when every run is work.
    demand: NonEmptyStr
    correlation: ModelAutomationCorrelation | None = None
    idle_runs_before_alarm: PositiveInt = 3
    positive_control: ModelAutomationPositiveControl
    state: EnumAutomationProcessState = EnumAutomationProcessState.ACTIVE

    @model_validator(mode="after")
    def _entry_shape(self) -> Self:
        kind = self.trigger.kind
        if kind in EVENT_TRIGGER_KINDS:
            if self.correlation is None:
                raise ValueError(
                    f"{self.process_id}: a {kind.value} entry needs a correlation"
                )
        elif self.expected_interval_seconds is None:
            raise ValueError(
                f"{self.process_id}: a {kind.value} entry needs "
                "expected_interval_seconds"
            )
        if kind not in DAEMON_TRIGGER_KINDS and self.max_runtime_seconds is None:
            raise ValueError(
                f"{self.process_id}: a {kind.value} entry needs max_runtime_seconds"
            )
        if self.emitter is EnumAutomationEmitter.OBSERVER and self.evidence is None:
            raise ValueError(
                f"{self.process_id}: an observer entry needs evidence to read"
            )
        if self.run_record is EnumAutomationRunRecord.LATEST_ONLY and (
            self.emitter is not EnumAutomationEmitter.OBSERVER
        ):
            raise ValueError(
                f"{self.process_id}: latest-only evidence is read by an observer; "
                "a self-emitting process reports every run"
            )
        if kind is EnumAutomationTriggerKind.CRON and (
            self.evidence is None or self.evidence.completion_record is None
        ):
            raise ValueError(
                f"{self.process_id}: a cron entry needs completion evidence "
                "(evidence.completion_record); a start record alone is refused"
            )
        interval = self.expected_interval_seconds
        if interval is not None:
            if self.max_silence_seconds is None:
                object.__setattr__(
                    self,
                    "max_silence_seconds",
                    2 * interval + trigger_kind_slack_seconds(kind, interval),
                )
            elif self.max_silence_seconds < interval:
                raise ValueError(
                    f"{self.process_id}: max_silence_seconds "
                    f"{self.max_silence_seconds} is below the interval {interval}, "
                    "so every healthy gap reads MISSED"
                )
        return self

    @property
    def declares_demand(self) -> bool:
        return self.demand != DEMAND_NONE

    def worst_case_seconds(
        self, timing: ModelAutomationLivenessTiming
    ) -> dict[EnumAutomationLivenessVerdict, int]:
        """Seconds from a failure's start to its delivered alarm, per verdict.

        Only verdicts this entry can reach are present. Each is the verdict's
        own bound plus ``timing.overhead_seconds``.
        """
        overhead = timing.overhead_seconds
        bounds: dict[EnumAutomationLivenessVerdict, int] = {
            EnumAutomationLivenessVerdict.UNOBSERVABLE: overhead,
        }
        if self.max_silence_seconds is not None:
            bounds[EnumAutomationLivenessVerdict.MISSED] = (
                self.max_silence_seconds + overhead
            )
        if self.max_runtime_seconds is not None:
            runtime = self.max_runtime_seconds + overhead
            bounds[EnumAutomationLivenessVerdict.FAILED] = runtime
            bounds[EnumAutomationLivenessVerdict.OVERRUN] = runtime
        elif self.max_silence_seconds is not None and self.declares_demand:
            # A daemon is OVERRUN when its progress stalls while demand waits.
            bounds[EnumAutomationLivenessVerdict.OVERRUN] = (
                self.max_silence_seconds + overhead
            )
        if self.declares_demand and self.expected_interval_seconds is not None:
            bounds[EnumAutomationLivenessVerdict.IDLE_WITH_DEMAND] = (
                self.idle_runs_before_alarm * self.expected_interval_seconds + overhead
            )
        if self.correlation is not None:
            bounds[EnumAutomationLivenessVerdict.TRIGGER_UNANSWERED] = (
                self.correlation.answer_deadline_seconds + overhead
            )
        return bounds

    def digest(self) -> str:
        """sha256 of the entry's canonical JSON; events carry it."""
        return _canonical_digest(self)


class ModelAutomationLivenessOverlay(BaseModel):
    """Every declared automatic process, with the monitor's timing."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={
            "x-onex-overlay-checks": [
                "deadline arithmetic: for every entry, the worst case of each "
                "reachable verdict (MISSED from max_silence_seconds, FAILED and "
                "OVERRUN from max_runtime_seconds, IDLE_WITH_DEMAND from "
                "idle_runs_before_alarm intervals, TRIGGER_UNANSWERED from the "
                "correlation's answer deadline, UNOBSERVABLE) plus the observer "
                "interval, the confirmation hold, one outbox retry and the "
                "delivery budget must not exceed alarm_deadline_seconds",
                "latest-only run records require expected_interval_seconds of at "
                "least three observer intervals",
                "a cron entry names its completion record",
                "process_id is unique",
            ]
        },
    )

    schema_version: Literal["automation-liveness-overlay/v1"]
    timing: ModelAutomationLivenessTiming = Field(
        default_factory=ModelAutomationLivenessTiming
    )
    #: Required, so an overlay that forgot its processes is refused rather than
    #: read as declaring none.
    processes: tuple[ModelAutomationLivenessEntry, ...]

    @model_validator(mode="after")
    def _overlay_checks(self) -> Self:
        seen: set[str] = set()
        for entry in self.processes:
            if entry.process_id in seen:
                raise ValueError(f"duplicate process_id {entry.process_id}")
            seen.add(entry.process_id)
            self._check_latest_only(entry)
            self._check_deadline(entry)
        return self

    def _check_latest_only(self, entry: ModelAutomationLivenessEntry) -> None:
        if entry.run_record is not EnumAutomationRunRecord.LATEST_ONLY:
            return
        floor = 3 * self.timing.observer_interval_seconds
        interval = entry.expected_interval_seconds
        if interval is None or interval < floor:
            raise ValueError(
                f"{entry.process_id}: latest-only evidence needs a cadence of at "
                f"least every third observer poll ({floor} s); got {interval}"
            )

    def _check_deadline(self, entry: ModelAutomationLivenessEntry) -> None:
        bounds = entry.worst_case_seconds(self.timing)
        over = {
            verdict: seconds
            for verdict, seconds in bounds.items()
            if seconds > entry.alarm_deadline_seconds
        }
        if over:
            detail = ", ".join(
                f"{verdict.value}={seconds} s"
                for verdict, seconds in sorted(over.items())
            )
            raise ValueError(
                f"{entry.process_id}: worst case past alarm_deadline_seconds "
                f"{entry.alarm_deadline_seconds}: {detail}"
            )

    def digest(self) -> str:
        """sha256 of the overlay's canonical JSON."""
        return _canonical_digest(self)


def load_automation_liveness_overlay(path: Path) -> ModelAutomationLivenessOverlay:
    """Read and validate an overlay YAML file."""
    return ModelAutomationLivenessOverlay.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def load_default_automation_liveness_overlay() -> ModelAutomationLivenessOverlay:
    """The shipped neutral overlay: no host, unit, lane or process."""
    text = (
        resources.files(_PACKAGE)
        .joinpath(_DEFAULT_OVERLAY_FILE)
        .read_text(encoding="utf-8")
    )
    return ModelAutomationLivenessOverlay.model_validate(yaml.safe_load(text))


# --------------------------------------------------------------------------- events


class ModelAutomationRunObserved(BaseModel):
    """One run's start or finish, from the process itself or an observer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    host: NonEmptyStr
    #: Stable per run, so a re-read never double-counts.
    run_id: NonEmptyStr
    phase: EnumAutomationRunPhase
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    outcome: EnumAutomationRunOutcome | None = None
    exit_code: int | None = None
    did_work_count: NonNegativeInt | None = None
    #: Null only when the entry declares ``demand: none``.
    demand_count: NonNegativeInt | None = None
    #: Runs that happened between two reads of ``latest-only`` evidence.
    unseen_runs: NonNegativeInt = 0
    work_unit: NonEmptyStr | None = None
    evidence_ref: NonEmptyStr
    emitter: EnumAutomationEmitter
    observed_at: AwareDatetime
    contract_digest: Sha256Hex

    @model_validator(mode="after")
    def _phase_shape(self) -> Self:
        if self.phase is EnumAutomationRunPhase.STARTED:
            if (
                self.finished_at is not None
                or self.outcome is not None
                or self.exit_code is not None
                or self.did_work_count is not None
            ):
                raise ValueError(
                    "a started run carries no finished_at, outcome, exit_code or "
                    "did_work_count"
                )
            return self
        if self.finished_at is None or self.outcome is None:
            raise ValueError("a finished run needs finished_at and outcome")
        if self.did_work_count is None:
            raise ValueError("a finished run needs did_work_count")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self


class ModelAutomationHeartbeat(BaseModel):
    """Progress of a daemon, an observer or a watchdog leg."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    host: NonEmptyStr
    pid: PositiveInt | None = None
    process_started_at: AwareDatetime
    #: Monotonic, counted by the worker loop; never the PID.
    progress_counter: NonNegativeInt
    last_progress_at: AwareDatetime | None = None
    demand_count: NonNegativeInt | None = None
    emitted_at: AwareDatetime
    # Watchdog legs only.
    selftest: EnumWatchdogSelftest | None = None
    consumed_offset: NonNegativeInt | None = None
    outbox_backlog: NonNegativeInt | None = None
    last_ledger_write_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _watchdog_fields_together(self) -> Self:
        leg = (self.selftest, self.consumed_offset, self.outbox_backlog)
        if any(v is not None for v in leg) and any(v is None for v in leg):
            raise ValueError(
                "a watchdog leg heartbeat carries selftest, consumed_offset and "
                "outbox_backlog together"
            )
        if self.selftest is None and self.last_ledger_write_at is not None:
            raise ValueError("last_ledger_write_at belongs to a watchdog leg heartbeat")
        return self


class ModelAutomationLivenessDeclared(BaseModel):
    """The overlay as the watchdog loaded it, on start and on every change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay: ModelAutomationLivenessOverlay
    overlay_digest: Sha256Hex
    declared_by: ProcessId
    host: NonEmptyStr
    declared_at: AwareDatetime

    @model_validator(mode="after")
    def _digest_matches(self) -> Self:
        if self.overlay_digest != self.overlay.digest():
            raise ValueError("overlay_digest does not match the overlay")
        return self


def _check_verdict_triplet(
    verdict: EnumAutomationLivenessVerdict,
    state: EnumLivenessState,
    reason: EnumAutomationLivenessReason,
) -> None:
    if state is not VERDICT_STATE[verdict]:
        raise ValueError(
            f"state {state.value} does not match verdict {verdict.value} "
            f"({VERDICT_STATE[verdict].value})"
        )
    if reason not in VERDICT_REASONS[verdict]:
        raise ValueError(f"reason {reason.value} does not belong to {verdict.value}")


class ModelAutomationLivenessVerdictEvent(BaseModel):
    """One per process per evaluation where the verdict changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: ProcessId
    host: NonEmptyStr
    verdict: EnumAutomationLivenessVerdict
    state: EnumLivenessState
    reason: EnumAutomationLivenessReason
    previous_verdict: EnumAutomationLivenessVerdict | None = None
    verdict_since: AwareDatetime
    evaluated_at: AwareDatetime
    #: The entry's digest; absent only for UNDECLARED, which has no entry.
    contract_digest: Sha256Hex | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_verdict_triplet(self.verdict, self.state, self.reason)
        if self.previous_verdict is self.verdict:
            raise ValueError("a verdict event is emitted only when the verdict changes")
        if self.verdict_since > self.evaluated_at:
            raise ValueError("verdict_since is after evaluated_at")
        undeclared = self.verdict is EnumAutomationLivenessVerdict.UNDECLARED
        if undeclared and self.contract_digest is not None:
            raise ValueError("an UNDECLARED process has no contract_digest")
        if not undeclared and self.contract_digest is None:
            raise ValueError(f"a {self.verdict.value} verdict needs contract_digest")
        return self


class ModelAutomationAlarmRaised(BaseModel):
    """An alarm episode opened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: UUID
    process_id: ProcessId
    host: NonEmptyStr
    verdict: EnumAutomationLivenessVerdict
    state: EnumLivenessState
    reason: EnumAutomationLivenessReason
    severity: EnumAlarmSeverity
    opened_at: AwareDatetime
    #: One due time, used by the deadline arithmetic, the outbox retry and the
    #: external escalation.
    delivery_due_at: AwareDatetime
    evidence_ref: NonEmptyStr
    #: The next step, machine-readable, e.g. ``diagnose:<process_id>``.
    action: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z-]*:\S+$")]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.verdict in PASSING_VERDICTS:
            raise ValueError(f"{self.verdict.value} never opens an alarm episode")
        _check_verdict_triplet(self.verdict, self.state, self.reason)
        if (
            self.severity is EnumAlarmSeverity.LOW
            and self.reason not in LOW_SEVERITY_REASONS
        ):
            raise ValueError(
                "low severity is only for a single failed run; "
                f"{self.verdict.value}/{self.reason.value} is high severity"
            )
        if self.delivery_due_at < self.opened_at:
            raise ValueError("delivery_due_at precedes opened_at")
        return self


class ModelAutomationAlarmCleared(BaseModel):
    """An alarm episode closed after its hysteresis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: UUID
    process_id: ProcessId
    host: NonEmptyStr
    verdict: EnumAutomationLivenessVerdict
    opened_at: AwareDatetime
    cleared_at: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.verdict in PASSING_VERDICTS:
            raise ValueError(f"{self.verdict.value} never opens an alarm episode")
        if self.cleared_at < self.opened_at:
            raise ValueError("cleared_at precedes opened_at")
        return self


class ModelAutomationAlarmDelivered(BaseModel):
    """One delivery attempt and its receipt or its failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: UUID
    route: EnumAlarmDeliveryRoute
    attempted_at: AwareDatetime
    delivered: bool
    #: The Slack message timestamp or the GitHub issue reference.
    message_ref: NonEmptyStr | None = None
    failure: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _receipt_or_failure(self) -> Self:
        if self.delivered:
            if self.message_ref is None or self.failure is not None:
                raise ValueError(
                    "a delivered alarm carries its message_ref and no failure"
                )
        elif self.failure is None or self.message_ref is not None:
            raise ValueError(
                "an undelivered alarm carries its failure and no message_ref"
            )
        return self


class ModelAutomationAlarmRecorded(BaseModel):
    """The ledger line the dead-man wrote for an opened or closed episode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: UUID
    recorded_at: AwareDatetime
    ledger_line: NonEmptyStr
    recorded_by: ProcessId


EVENT_PAYLOAD_MODELS: Mapping[EnumAutomationLivenessEvent, type[BaseModel]] = {
    EnumAutomationLivenessEvent.RUN_OBSERVED: ModelAutomationRunObserved,
    EnumAutomationLivenessEvent.HEARTBEAT: ModelAutomationHeartbeat,
    EnumAutomationLivenessEvent.LIVENESS_DECLARED: ModelAutomationLivenessDeclared,
    EnumAutomationLivenessEvent.LIVENESS_VERDICT: ModelAutomationLivenessVerdictEvent,
    EnumAutomationLivenessEvent.ALARM_RAISED: ModelAutomationAlarmRaised,
    EnumAutomationLivenessEvent.ALARM_CLEARED: ModelAutomationAlarmCleared,
    EnumAutomationLivenessEvent.ALARM_DELIVERED: ModelAutomationAlarmDelivered,
    EnumAutomationLivenessEvent.ALARM_RECORDED: ModelAutomationAlarmRecorded,
}


@cache
def automation_liveness_topics() -> Mapping[EnumAutomationLivenessEvent, str]:
    """Topic per event, read from the seam's topic contract."""
    text = (
        resources.files(_PACKAGE)
        .joinpath(_TOPIC_CONTRACT_FILE)
        .read_text(encoding="utf-8")
    )
    data = yaml.safe_load(text)
    declared = data["event_topics"]
    topics = {
        EnumAutomationLivenessEvent(key): str(spec["topic"])
        for key, spec in declared.items()
    }
    missing = set(EnumAutomationLivenessEvent) - set(topics)
    if missing:
        raise ValueError(
            f"{_TOPIC_CONTRACT_FILE} declares no topic for "
            f"{sorted(m.value for m in missing)}"
        )
    return MappingProxyType(topics)
