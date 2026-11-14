# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay and result models of the convergence orchestrator node (OMN-24198).

One scheduled run sends one convergence command per declared host (a lab work unit) and ends with one
typed result per host. Each host result carries the clones the host command named, so a clone that
cannot be converged is reported by name with its reason. The overlay's ``mode`` decides which command
the run sends: ``observe`` sends the read-only command and changes nothing, ``act`` sends the command
that converges.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_host_hygiene_orchestrator import (
    HOST_NAME_PATTERN,
    ModelHostHygieneLabWork,
)
from omnimarket.models.model_lab_work_unit import MAX_TIMEOUT_SECONDS, WorkKind
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumConvergenceMode(StrEnum):
    """Whether a run may change a clone."""

    OBSERVE = "observe"
    ACT = "act"


class EnumConvergenceBackend(StrEnum):
    IN_MEMORY = "in_memory"
    LAB_WORK = "lab_work"


class ModelConvergenceCommand(_Frozen):
    """The argv of one mode's host command and how long the host may run it."""

    argv: Annotated[tuple[str, ...], Field(min_length=1)]
    timeout_seconds: int = Field(ge=1, le=MAX_TIMEOUT_SECONDS)
    kind: WorkKind = "other"


class ModelConvergenceHostRow(_Frozen):
    """One host the run converges: its name and the registry root its clones sit under."""

    name: Annotated[str, Field(pattern=HOST_NAME_PATTERN)]
    registry_root: Annotated[str, Field(min_length=1)]


class ModelConvergenceOrchestratorOverlay(ModelScheduleDeclaration):
    """The orchestrator node's overlay: its cadence, mode, backend, commands and hosts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EnumConvergenceMode
    backend: EnumConvergenceBackend
    max_parallel_units: int = Field(ge=1, le=16)
    lab_work: ModelHostHygieneLabWork
    observe_command: ModelConvergenceCommand
    act_command: ModelConvergenceCommand
    hosts: Annotated[tuple[ModelConvergenceHostRow, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _hosts_unique(self) -> ModelConvergenceOrchestratorOverlay:
        names = [h.name for h in self.hosts]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate host(s): {', '.join(dupes)}")
        return self

    @property
    def command(self) -> ModelConvergenceCommand:
        """The command this run's mode sends."""
        return (
            self.observe_command
            if self.mode is EnumConvergenceMode.OBSERVE
            else self.act_command
        )


class EnumConvergenceCloneStatus(StrEnum):
    """What the host command said about one clone."""

    CLEAN = "clean"
    WOULD_MOVE = "would_move"
    WOULD_REFUSE = "would_refuse"
    CONVERGED = "converged"
    SKIPPED = "skipped"
    REFUSED = "refused"
    FAILED = "failed"


#: The clone statuses that are a typed refusal: the clone holds local work and is not converged.
REFUSAL_STATUSES = frozenset(
    {EnumConvergenceCloneStatus.WOULD_REFUSE, EnumConvergenceCloneStatus.REFUSED}
)


class ModelConvergenceCloneFinding(_Frozen):
    """One clone a host command named, with the reason it gave."""

    clone: str = Field(min_length=1)
    status: EnumConvergenceCloneStatus
    reason: str = Field(default="", max_length=2000)


class EnumConvergenceHostOutcome(StrEnum):
    #: No clone needed anything.
    CLEAN = "clean"
    #: Act: at least one clone was converged and none was refused or failed.
    CONVERGED = "converged"
    #: Observe: at least one clone would be moved by an act run; none was refused or failed.
    OBSERVED_PENDING = "observed_pending"
    #: At least one clone holds local work and was not converged; the findings name it and the reason.
    REFUSED = "refused"
    #: The command exited non-zero, a clone read failed, or its output named nothing it could be read from.
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"


class ModelConvergenceHostResult(_Frozen):
    """The typed result of one host's convergence command."""

    host: str
    mode: EnumConvergenceMode
    work_unit_id: str
    outcome: EnumConvergenceHostOutcome
    exit_code: int | None = None
    clones: tuple[ModelConvergenceCloneFinding, ...] = ()
    detail: str = Field(default="", max_length=2000)
    output_tail: str = Field(default="", max_length=2000)

    @property
    def refusals(self) -> tuple[ModelConvergenceCloneFinding, ...]:
        """The clones this host refused, each with its reason."""
        return tuple(c for c in self.clones if c.status in REFUSAL_STATUSES)


#: The host outcomes under which a run still ends completed.
COMPLETED_OUTCOMES = frozenset(
    {
        EnumConvergenceHostOutcome.CLEAN,
        EnumConvergenceHostOutcome.CONVERGED,
        EnumConvergenceHostOutcome.OBSERVED_PENDING,
    }
)


class EnumConvergenceRunOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class ModelConvergenceRunCompleted(_Frozen):
    """The result of one orchestrator run: one typed result per declared host."""

    run_id: str
    job_id: str
    tick_id: str
    mode: EnumConvergenceMode
    started_at: datetime
    finished_at: datetime
    outcome: EnumConvergenceRunOutcome
    hosts: tuple[ModelConvergenceHostResult, ...]


def run_outcome(
    hosts: tuple[ModelConvergenceHostResult, ...],
) -> EnumConvergenceRunOutcome:
    """Completed only when every host's result is a completed one; a refusal or failure fails the run."""
    if hosts and all(h.outcome in COMPLETED_OUTCOMES for h in hosts):
        return EnumConvergenceRunOutcome.COMPLETED
    return EnumConvergenceRunOutcome.FAILED


__all__: list[str] = [
    "COMPLETED_OUTCOMES",
    "REFUSAL_STATUSES",
    "EnumConvergenceBackend",
    "EnumConvergenceCloneStatus",
    "EnumConvergenceHostOutcome",
    "EnumConvergenceMode",
    "EnumConvergenceRunOutcome",
    "ModelConvergenceCloneFinding",
    "ModelConvergenceCommand",
    "ModelConvergenceHostResult",
    "ModelConvergenceHostRow",
    "ModelConvergenceOrchestratorOverlay",
    "ModelConvergenceRunCompleted",
    "run_outcome",
]
