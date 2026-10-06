# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enums of the lab job supervisor: job states, kinds, events and intents."""

from __future__ import annotations

from enum import StrEnum


class EnumLabJobState(StrEnum):
    """States of one lab job. ``done`` and ``alerted`` end an episode."""

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    CHECKING = "checking"
    STOPPING = "stopping"
    RETRYING = "retrying"
    STALLED = "stalled"
    FAILED = "failed"
    ALERTING = "alerting"
    ALERTED = "alerted"
    DONE = "done"


TERMINAL_LAB_JOB_STATES: frozenset[EnumLabJobState] = frozenset(
    {EnumLabJobState.DONE, EnumLabJobState.ALERTED}
)


class EnumLabJobKind(StrEnum):
    """What the job runs. ``adopted`` is minted by the supervisor for a CLAIM."""

    LANE = "lane"
    WORK_UNIT = "work_unit"
    DELEGATION = "delegation"
    ADOPTED = "adopted"


class EnumLabJobEngine(StrEnum):
    """The remote-lane runner's own ``--model`` values, plus ``shell``."""

    OPUS = "opus"
    SONNET = "sonnet"
    CODEX = "codex"
    GLM = "glm"
    CODE_EDIT = "code-edit"
    SHELL = "shell"


class EnumLabJobOnTimeBox(StrEnum):
    """What a time-box failure does: a continuation restart, or fail."""

    CONTINUE = "continue"
    FAIL = "fail"


class EnumLabJobDoneCriterionKind(StrEnum):
    """Kinds of done criterion; every criterion of a job must hold."""

    TERMINAL_ROW = "terminal_row"
    PR_MERGED = "pr_merged"
    CHECK_PASSING = "check_passing"
    EXIT_ZERO = "exit_zero"


class EnumLabJobLiveness(StrEnum):
    """The five verdicts of ``HandlerLaneLiveness``."""

    ALIVE = "alive"
    DROPPED = "dropped"
    TERMINATED = "terminated"
    UNOBSERVABLE = "unobservable"
    UNKNOWN_RELAY_SILENT = "unknown_relay_silent"


class EnumLabJobAttemptOutcome(StrEnum):
    """How an attempt's process ended, from the unit terminal or the runner."""

    EXITED_ZERO = "exited_zero"
    EXITED_NONZERO = "exited_nonzero"
    TIMED_OUT = "timed_out"
    INFRA_ERROR = "infra_error"


class EnumLabJobEventKind(StrEnum):
    """Normalised events the reducer takes, one per call."""

    SUBMITTED = "submitted"
    ADOPTED = "adopted"
    TAKEN = "taken"
    DECLINED = "declined"
    CLAIMED = "claimed"
    CHECKED = "checked"
    ATTEMPT_ENDED = "attempt_ended"
    DONE_EVALUATED = "done_evaluated"
    STOP_CONFIRMED = "stop_confirmed"
    ALERT_ACKNOWLEDGED = "alert_acknowledged"
    TICK = "tick"
    CANCELLED = "cancelled"
    RESOLVED = "resolved"


class EnumLabJobIntentKind(StrEnum):
    """Intents the reducer asks the orchestrator to publish."""

    DISPATCH = "dispatch"
    TAKE_REFUSED = "take_refused"
    KILL_ATTEMPT = "kill_attempt"
    ALERT = "alert"


class EnumLabJobResolution(StrEnum):
    """An operator's resolution of an alerted job."""

    RETRY = "retry"
    CLOSE = "close"


__all__: list[str] = [
    "TERMINAL_LAB_JOB_STATES",
    "EnumLabJobAttemptOutcome",
    "EnumLabJobDoneCriterionKind",
    "EnumLabJobEngine",
    "EnumLabJobEventKind",
    "EnumLabJobIntentKind",
    "EnumLabJobKind",
    "EnumLabJobLiveness",
    "EnumLabJobOnTimeBox",
    "EnumLabJobResolution",
    "EnumLabJobState",
]
