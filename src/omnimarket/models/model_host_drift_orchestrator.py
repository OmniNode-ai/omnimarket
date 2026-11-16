# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay and result models of the host drift orchestrator node (OMN-24197).

The overlay declares the cadence, the lab-work block the units are published through, the one drift check
command every host runs and the hosts it runs on. The result models carry one run back: one verdict per
declared host, and the run outcome derived from them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_lab_work_unit import MAX_TIMEOUT_SECONDS, WorkKind
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)

HOST_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
COMMIT_SHA_PATTERN = r"^[0-9a-f]{40}$"
LANE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
UNIT_ID_PREFIX_PATTERN = r"^[a-z][a-z0-9-]{0,15}$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumHostDriftBackend(StrEnum):
    IN_MEMORY = "in_memory"
    LAB_WORK = "lab_work"


class ModelHostDriftLabWork(_Frozen):
    """The lab-work block: where units are published and how they are identified."""

    repo: Annotated[str, Field(pattern=REPO_PATTERN)]
    commit_sha: Annotated[str, Field(pattern=COMMIT_SHA_PATTERN)]
    lane: Annotated[str, Field(pattern=LANE_PATTERN)]
    unit_id_prefix: Annotated[str, Field(pattern=UNIT_ID_PREFIX_PATTERN)]
    kafka_bootstrap: Annotated[str | None, Field(min_length=1)] = None
    bus_lane: Annotated[str | None, Field(min_length=1)] = None
    workspace_root: str | None = None

    @model_validator(mode="after")
    def _one_address(self) -> ModelHostDriftLabWork:
        if self.kafka_bootstrap is not None and self.bus_lane is not None:
            raise ValueError("names both a kafka_bootstrap and a bus_lane")
        if self.kafka_bootstrap is None and self.bus_lane is None:
            raise ValueError("names neither a kafka_bootstrap nor a bus_lane")
        return self


class ModelHostDriftCheck(_Frozen):
    """The command every declared host runs: argv words, with {host}, {registry_root} and {profiles_path} filled per host."""

    argv: Annotated[tuple[str, ...], Field(min_length=1)]
    kind: WorkKind = "other"


class ModelHostDriftHostRow(_Frozen):
    """One host the orchestrator checks."""

    name: Annotated[str, Field(pattern=HOST_NAME_PATTERN)]
    registry_root: Annotated[str, Field(min_length=1)]
    profiles_path: Annotated[str, Field(min_length=1)]


class ModelHostDriftOrchestratorOverlay(ModelScheduleDeclaration):
    """The orchestrator node's overlay: its cadence, backend and all configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumHostDriftBackend
    result_timeout_seconds: int = Field(ge=1, le=MAX_TIMEOUT_SECONDS)
    max_parallel_units: int = Field(ge=1, le=16)
    lab_work: ModelHostDriftLabWork
    check: ModelHostDriftCheck
    hosts: Annotated[tuple[ModelHostDriftHostRow, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _hosts_unique(self) -> ModelHostDriftOrchestratorOverlay:
        names = [h.name for h in self.hosts]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate host(s): {', '.join(dupes)}")
        return self


class EnumHostDriftHostVerdict(StrEnum):
    """What one host's check came to. IN_SYNC and DRIFTED are answers; the rest are not."""

    IN_SYNC = "in_sync"
    DRIFTED = "drifted"
    CHECK_FAILED = "check_failed"
    NO_RESULT = "no_result"
    REFUSED = "refused"


class ModelHostDriftHostVerdict(_Frozen):
    """The verdict for one declared host: the typed outcome, the exit code and the tail of the check's output."""

    host: str
    verdict: EnumHostDriftHostVerdict
    work_unit_id: str
    exit_code: int | None = None
    detail: str = Field(default="", max_length=2000)
    output_tail: str = Field(default="", max_length=2000)


class EnumHostDriftRunOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class ModelHostDriftRunCompleted(_Frozen):
    """One run of the orchestrator: one verdict per declared host."""

    run_id: str
    job_id: str
    tick_id: str
    started_at: datetime
    finished_at: datetime
    outcome: EnumHostDriftRunOutcome
    verdicts: tuple[ModelHostDriftHostVerdict, ...]
    failed_hosts: tuple[str, ...] = ()


_ANSWERED = frozenset(
    {EnumHostDriftHostVerdict.IN_SYNC, EnumHostDriftHostVerdict.DRIFTED}
)


def run_outcome(
    verdicts: tuple[ModelHostDriftHostVerdict, ...],
) -> EnumHostDriftRunOutcome:
    """A run is completed when every host answered with in_sync or drifted; one host without an answer fails it."""
    if verdicts and all(v.verdict in _ANSWERED for v in verdicts):
        return EnumHostDriftRunOutcome.COMPLETED
    return EnumHostDriftRunOutcome.FAILED


def failed_hosts(verdicts: tuple[ModelHostDriftHostVerdict, ...]) -> tuple[str, ...]:
    """The hosts that gave no answer, in declared order."""
    return tuple(v.host for v in verdicts if v.verdict not in _ANSWERED)


__all__: list[str] = [
    "EnumHostDriftBackend",
    "EnumHostDriftHostVerdict",
    "EnumHostDriftRunOutcome",
    "ModelHostDriftCheck",
    "ModelHostDriftHostRow",
    "ModelHostDriftHostVerdict",
    "ModelHostDriftLabWork",
    "ModelHostDriftOrchestratorOverlay",
    "ModelHostDriftRunCompleted",
    "failed_hosts",
    "run_outcome",
]
