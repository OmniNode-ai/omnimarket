# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay and result models for the host hygiene orchestrator node (OMN-23707, part A).

The orchestrator node's overlay declares its cadence, the lab-work block it
publishes units through, the cleanup policies it runs, and the hosts it
watches. The result models carry one run's outcomes back to the caller.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_host_hygiene import (
    ModelHostHygieneCleanupPolicy,
    ModelHostHygieneDecisionItem,
)
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


class ModelHostHygieneCleanupDeclaration(ModelHostHygieneCleanupPolicy):
    """A cleanup policy with the argv words and timeout for the lab-work unit."""

    argv: Annotated[tuple[str, ...], Field(min_length=1)]
    timeout_seconds: int = Field(ge=1, le=MAX_TIMEOUT_SECONDS)
    kind: WorkKind = "other"


class ModelHostHygieneHostRow(_Frozen):
    """One host the orchestrator watches: its name, home, pressure volume, and cleanups."""

    name: Annotated[str, Field(pattern=HOST_NAME_PATTERN)]
    registry_root: Annotated[str, Field(min_length=1)]
    pressure_volume: Annotated[str, Field(min_length=1)]
    cleanup_ids: Annotated[tuple[str, ...], Field(min_length=1)]


class EnumHostHygieneBackend(StrEnum):
    IN_MEMORY = "in_memory"
    LAB_WORK = "lab_work"


class ModelHostHygieneLabWork(_Frozen):
    """The lab-work block: where units are published and how they are identified."""

    repo: Annotated[str, Field(pattern=REPO_PATTERN)]
    commit_sha: Annotated[str, Field(pattern=COMMIT_SHA_PATTERN)]
    lane: Annotated[str, Field(pattern=LANE_PATTERN)]
    unit_id_prefix: Annotated[str, Field(pattern=UNIT_ID_PREFIX_PATTERN)]
    kafka_bootstrap: Annotated[str | None, Field(min_length=1)] = None
    bus_lane: Annotated[str | None, Field(min_length=1)] = None
    workspace_root: str | None = None

    @model_validator(mode="after")
    def _one_address(self) -> ModelHostHygieneLabWork:
        if self.kafka_bootstrap is not None and self.bus_lane is not None:
            raise ValueError("names both a kafka_bootstrap and a bus_lane")
        if self.kafka_bootstrap is None and self.bus_lane is None:
            raise ValueError("names neither a kafka_bootstrap nor a bus_lane")
        return self


class ModelHostHygieneOrchestratorOverlay(ModelScheduleDeclaration):
    """The orchestrator node's overlay: its cadence, backend, and all configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumHostHygieneBackend
    max_reading_age_seconds: int = Field(gt=0)
    max_parallel_units: int = Field(ge=1, le=16)
    lab_work: ModelHostHygieneLabWork
    cleanups: Annotated[
        tuple[ModelHostHygieneCleanupDeclaration, ...], Field(min_length=1)
    ]
    hosts: Annotated[tuple[ModelHostHygieneHostRow, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _validate(self) -> ModelHostHygieneOrchestratorOverlay:
        # hosts unique
        host_names = [h.name for h in self.hosts]
        if len(set(host_names)) != len(host_names):
            dupes = sorted({h for h in host_names if host_names.count(h) > 1})
            raise ValueError(f"duplicate host(s): {', '.join(dupes)}")
        # cleanup ids unique
        cleanup_ids = [c.cleanup_id for c in self.cleanups]
        if len(set(cleanup_ids)) != len(cleanup_ids):
            dupes = sorted({c for c in cleanup_ids if cleanup_ids.count(c) > 1})
            raise ValueError(f"duplicate cleanup id(s): {', '.join(dupes)}")
        # every host cleanup id is declared
        declared = set(cleanup_ids)
        for h in self.hosts:
            for cid in h.cleanup_ids:
                if cid not in declared:
                    raise ValueError(
                        f"host {h.name!r} declares unknown cleanup id {cid!r}"
                    )
        return self


class EnumHostHygieneUnitOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    REFUSED = "refused"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"


class ModelHostHygieneUnitResult(_Frozen):
    """The result of one dispatched unit."""

    host: str
    cleanup_id: str
    work_unit_id: str
    outcome: EnumHostHygieneUnitOutcome
    exit_code: int | None = None
    detail: str = Field(default="", max_length=2000)
    output_tail: str = Field(default="", max_length=2000)


class EnumHostHygieneRunOutcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    NOTHING_DUE = "nothing_due"


class ModelHostHygieneRunCompleted(_Frozen):
    """The result of one orchestrator run."""

    run_id: str
    job_id: str
    tick_id: str
    started_at: datetime
    finished_at: datetime
    outcome: EnumHostHygieneRunOutcome
    decided: tuple[ModelHostHygieneDecisionItem, ...]
    units: tuple[ModelHostHygieneUnitResult, ...]


def run_outcome(
    units: tuple[ModelHostHygieneUnitResult, ...],
) -> EnumHostHygieneRunOutcome:
    """Derive the run outcome from the unit results."""
    if not units:
        return EnumHostHygieneRunOutcome.NOTHING_DUE
    outcomes = [u.outcome for u in units]
    if all(o is EnumHostHygieneUnitOutcome.COMPLETED for o in outcomes):
        return EnumHostHygieneRunOutcome.COMPLETED
    if all(o is not EnumHostHygieneUnitOutcome.COMPLETED for o in outcomes):
        return EnumHostHygieneRunOutcome.FAILED
    return EnumHostHygieneRunOutcome.PARTIAL


__all__: list[str] = [
    "EnumHostHygieneBackend",
    "EnumHostHygieneRunOutcome",
    "EnumHostHygieneUnitOutcome",
    "ModelHostHygieneCleanupDeclaration",
    "ModelHostHygieneHostRow",
    "ModelHostHygieneLabWork",
    "ModelHostHygieneOrchestratorOverlay",
    "ModelHostHygieneRunCompleted",
    "ModelHostHygieneUnitResult",
    "run_outcome",
]
