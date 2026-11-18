# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the commitment check runner: its rules, its command, one check's execution and the run's result."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.commitment import (
    EnumCommitmentStatus,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
    ModelLedgerAppendRoute,
)


class ModelCommitmentCheckHost(BaseModel):
    """A lab host that runs the check runner on its schedule (a systemd user timer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    # The venv whose interpreter runs the installed unit, relative to the registry root (or absolute).
    venv: str = Field(min_length=1)
    # Environment the host's installed unit carries (the ledger routes, the overlay file pins, the host name).
    environment: dict[str, str]


class ModelCommitmentCheckRules(BaseModel):
    """The overlay of the runner. An absent key refuses the load and names it; no value is a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The lane the runner's reading rows are written as.
    runner_lane: str = Field(min_length=1)
    # A check runs only when its argv[0] is one of these exact names; any other is refused and never executed.
    allowed_executables: tuple[str, ...] = Field(min_length=1)
    check_timeout_seconds: int = Field(ge=1)
    # A verified commitment is checked again once its newest reading is this old.
    recheck_interval_seconds: int = Field(ge=1)
    max_checks_per_run: int = Field(ge=1)
    ledger_lookback_hours: float = Field(gt=0)
    output_tail_chars: int = Field(ge=0)
    # Where commitment rows are read and where readings are appended: the reading writer's routes, named by variable.
    ledger_read: Annotated[
        ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
        Field(discriminator="route"),
    ]
    ledger_append: ModelLedgerAppendRoute
    # The hosts `schedule install --host <name>` installs the timer on; it refuses any other. Empty: the runner runs by hand.
    schedule_hosts: tuple[ModelCommitmentCheckHost, ...]

    def host(self, name: str) -> ModelCommitmentCheckHost | None:
        return next((h for h in self.schedule_hosts if h.name == name), None)


class ModelCommitmentCheckCommand(BaseModel):
    """One run: every due commitment's check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)


class ModelCheckExecution(BaseModel):
    """What running one check gave: its exit code (None when it never exited) and the tail of its output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None
    timed_out: bool = False
    output_tail: str = ""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class ModelCommitmentCheckOutcome(BaseModel):
    """One commitment's check in this run and what it changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commitment_id: str
    status_before: EnumCommitmentStatus
    status_after: EnumCommitmentStatus
    # ran: the check executed. verified | regressed | reopened-passes | still-open | refused:<cause> | append-failed:<cause>
    ran: bool
    exit_code: int | None = None
    result: str
    row: str | None = None


class ModelCommitmentCheckResult(BaseModel):
    """The run: its outcomes, or the failure that stopped it (a ledger that could not be read is never an empty run)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    failure: str | None = None
    ran_at: datetime
    outcomes: tuple[ModelCommitmentCheckOutcome, ...] = ()
    # Commitment rows that did not read (named by the ledger node's fold).
    malformed: tuple[str, ...] = ()
    # Due commitments this run did not reach (the cap or the service deadline); the next run takes them.
    deferred: int = 0
    # Every commitment the fold returned, withdrawn ones included.
    read: int = Field(ge=0, default=0)
    # Those whose status is not EnumCommitmentStatus.WITHDRAWN.
    live: int = Field(ge=0, default=0)
