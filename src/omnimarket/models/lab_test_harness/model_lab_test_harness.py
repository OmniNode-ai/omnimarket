# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the central test harness: its overlay, one run, and its reading.

The harness sends test cases to the lab work unit node on the declared bus lane and
turns the receipts into one reading. A golden case is a command that must pass. An
error case is a broken input that must go red: a pass there means the harness cannot
tell a broken run from a good one, so the reading is NOT WORKING.

The reading carries ``probed_at``, ``published_at`` and ``max_age_seconds`` so a reader
can apply the staleness rule from the row alone. ``tag`` keeps drill rows apart from
operational ones.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.model_runtime_tick_schedule import ModelRuntimeTickScheduleConfig

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def _plain(value: str) -> str:
    if not _NAME.match(value):
        raise ValueError(f"not a plain name: {value!r}")
    return value


class EnumLabTestHarnessKind(StrEnum):
    """Which result the case must produce for the harness to be working."""

    GOLDEN = "golden"
    ERROR = "error"


class EnumLabTestHarnessVerdict(StrEnum):
    """What a case's receipt says: the command ran and passed, ran and failed, or did not run."""

    PASS = "pass"
    FAIL = "fail"
    INFRA_ERROR = "infra_error"


class EnumLabTestHarnessReadingState(StrEnum):
    """WORKING, NOT WORKING or UNKNOWN. Anything but WORKING blocks admission."""

    WORKING = "WORKING"
    NOT_WORKING = "NOT WORKING"
    UNKNOWN = "UNKNOWN"


class ModelLabTestHarnessCase(BaseModel):
    """One command at one pushed commit on one named lab host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    kind: EnumLabTestHarnessKind
    repo: str
    commit_sha: str
    argv: tuple[str, ...] = Field(min_length=1, max_length=256)
    target_host: str
    timeout_seconds: int = Field(default=120, ge=1, le=7200)
    need_tools: tuple[str, ...] = ()

    @field_validator("case_id", "target_host")
    @classmethod
    def _check_plain(cls, value: str) -> str:
        return _plain(value)

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        if not _REPO.match(value):
            raise ValueError(f"repo must be owner/name: {value!r}")
        return value

    @field_validator("commit_sha")
    @classmethod
    def _check_sha(cls, value: str) -> str:
        if not _SHA.match(value):
            raise ValueError("commit_sha must be a full 40-hex sha")
        return value


class ModelLabTestHarnessScheduleHost(BaseModel):
    """A host the harness's own timer is declared for, and the venv its unit runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: The venv whose interpreter runs the unit, relative to the registry root (or absolute).
    venv: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _plain(value)


class ModelLabTestHarnessLedgerRows(BaseModel):
    """The ledger rows that carry the chain events to a floor on another host (OMN-23007).

    The floor draws a piece's golden and error events from the ledger rows its ``piece_events`` rule names, so a harness
    that runs on a host other than the floor's writes one row a run (and one for the planted failure) through the bus
    append command the environment variable ``command_env`` names.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    actor: str = Field(min_length=1)
    command_env: str = Field(min_length=1)
    host_env: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)

    @field_validator("lane")
    @classmethod
    def _check_lane(cls, value: str) -> str:
        return _plain(value)


class ModelLabTestHarnessEmission(BaseModel):
    """Where and as whom the harness emits its golden and error chain events (OMN-23007).

    The journal is the one the floor folds into receipts: ``journal_relpath`` is relative to the directory the
    environment variable ``home_env`` names. ``piece_number`` is the floor piece the chains belong to and
    ``chain_node`` the node name they carry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    journal_relpath: str = Field(min_length=1)
    piece_number: int = Field(ge=1, le=18)
    chain_node: str = Field(min_length=1)
    #: The ledger rows of the same events, for a floor on another host. Absent means the journal alone.
    ledger: ModelLabTestHarnessLedgerRows | None = None


class ModelLabTestHarnessOverlay(BaseModel):
    """Every setting of the harness. The node ships none of these values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_revision: str = Field(min_length=1)
    #: The floor row this harness feeds.
    piece: str
    #: ``drill`` or ``operational``: the two kinds of row are kept apart.
    tag: str
    #: The declared bus lane the work units travel on. Absent means the in-process bus,
    #: which has no lab host, so only a test that supplies its own opener runs on it.
    bus_lane: str | None = None
    #: The lane name the work units carry for attribution.
    lane: str
    schedule: ModelRuntimeTickScheduleConfig
    #: A reading older than this is NOT WORKING.
    max_age_seconds: int = Field(gt=0)
    #: How long past a unit's own limit the caller waits for its receipt.
    receipt_slack_seconds: int = Field(ge=0, le=3600)
    #: Where the current reading is written for the floor to read.
    current_result_path: str = Field(min_length=1)
    cases: tuple[ModelLabTestHarnessCase, ...] = Field(min_length=2)
    #: Hosts the harness's own systemd user timer is installed on (``schedule install --host``).
    #: Empty means no host declares the timer.
    schedule_hosts: tuple[ModelLabTestHarnessScheduleHost, ...] = ()
    #: Environment the installed unit carries (the state root, the registry root, the workspace
    #: config root): the unit holds no setting of its own.
    unit_environment: dict[str, str] = Field(default_factory=dict)
    #: The floor's emission journal and the chain identity. Absent means the harness emits no chain events.
    emission: ModelLabTestHarnessEmission | None = None

    @field_validator("piece", "tag", "lane")
    @classmethod
    def _check_plain(cls, value: str) -> str:
        return _plain(value)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        kinds = {case.kind for case in self.cases}
        if kinds != set(EnumLabTestHarnessKind):
            raise ValueError(
                "the harness needs at least one golden case and one error case"
            )
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        hosts = [host.name for host in self.schedule_hosts]
        if len(hosts) != len(set(hosts)):
            raise ValueError("a schedule host is declared twice")
        return self

    @property
    def bus_kind(self) -> Literal["kafka", "inmemory"]:
        """The bus the lane names: a declared lane is the broker, no lane the in-process bus."""
        return "kafka" if self.bus_lane else "inmemory"


class ModelLabTestHarnessRequested(BaseModel):
    """One run asked for, by the schedule or by hand."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    requested_at: datetime
    trigger: str = Field(min_length=1)

    @field_validator("run_id")
    @classmethod
    def _check_plain(cls, value: str) -> str:
        return _plain(value)


class ModelLabTestHarnessCaseOutcome(BaseModel):
    """What one case did, and whether that is what the harness needs it to do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    kind: EnumLabTestHarnessKind
    work_unit_id: str
    host: str
    verdict: EnumLabTestHarnessVerdict
    exit_code: int | None = None
    as_expected: bool
    duration_seconds: float = 0.0
    log_path: str = ""
    detail: str = Field(default="", max_length=2000)


class ModelLabTestHarnessResult(BaseModel):
    """All outcomes of one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    piece: str
    tag: str
    overlay_revision: str
    started_at: datetime
    finished_at: datetime
    outcomes: tuple[ModelLabTestHarnessCaseOutcome, ...]


class ModelLabTestHarnessReading(BaseModel):
    """The current result of the harness, in the shape the floor reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: str
    tag: str
    state: EnumLabTestHarnessReadingState
    #: True for every state but WORKING: UNKNOWN and stale count as NOT WORKING.
    blocks_admission: bool
    reason: str
    run_id: str | None = None
    overlay_revision: str | None = None
    probed_at: datetime | None = None
    published_at: datetime
    max_age_seconds: int = Field(gt=0)
    outcomes: tuple[ModelLabTestHarnessCaseOutcome, ...] = ()


class ModelLabTestHarnessFreshnessRequest(BaseModel):
    """The stored reading (or none) and the time it is read at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: str
    tag: str
    max_age_seconds: int = Field(gt=0)
    now: datetime
    stored: ModelLabTestHarnessReading | None = None
