# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, state and result models of the run engine effect node.

A run is a keyed, ordered list of steps. A step is a plain command (an argument vector, never a shell string, run with
no model provider), a wait on a named condition, or nothing else. The engine sequences steps and holds their order and
waits across process death; it decides nothing about what lands. Every command step is handed its idempotency key in
``ONEX_RUN_STEP_KEY`` (``<run_key>/<step name>``); the engine delivers a step at least once, so the command owns
making a second delivery of the same key a no-op.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

RUN_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._#:/@-]{0,255}$"
STEP_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
CONDITION_PATTERN = r"^[a-z][a-z0-9_.-]{0,127}$"
RELEASE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._#:/@-]{0,255}$"


class EnumRunStepKind(StrEnum):
    """What a step does."""

    COMMAND = "command"
    WAIT_RELEASE = "wait_release"


class EnumRunStatus(StrEnum):
    """Where a run is, read from the engine's own record."""

    NOT_FOUND = "not_found"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EnumStartOutcome(StrEnum):
    """The answer to a start: the three outcomes the existing protocols cannot return."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    UNKNOWN = "unknown"


class EnumReleaseOutcome(StrEnum):
    """The answer to a release of a named wait."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE = "stale"
    UNKNOWN_RUN = "unknown_run"
    RUN_FINISHED = "run_finished"
    UNKNOWN = "unknown"


class EnumCancelOutcome(StrEnum):
    """The answer to a cancel."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    UNKNOWN_RUN = "unknown_run"
    UNKNOWN = "unknown"


class EnumRunEngineOperation(StrEnum):
    """The operations the node routes."""

    START_RUN = "start_run"
    RELEASE_RUN = "release_run"
    CANCEL_RUN = "cancel_run"
    GET_RUN_STATE = "get_run_state"
    HEALTH = "health"


class ModelRunStepSpec(BaseModel):
    """One step of a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=STEP_NAME_PATTERN)
    kind: EnumRunStepKind
    argv: tuple[str, ...] = ()
    condition: str | None = Field(default=None, pattern=CONDITION_PATTERN)

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> ModelRunStepSpec:
        if self.kind is EnumRunStepKind.COMMAND:
            if not self.argv or not all(part for part in self.argv):
                raise ValueError("a command step needs a non-empty argument vector")
            if self.condition is not None:
                raise ValueError("a command step carries no condition")
        else:
            if self.argv:
                raise ValueError("a wait step carries no argument vector")
            if self.condition is None:
                raise ValueError("a wait step names its condition")
        return self


class ModelRunSpec(BaseModel):
    """A keyed run: the key is the run's identity, so a second start of the same key is a duplicate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_key: str = Field(pattern=RUN_KEY_PATTERN)
    steps: tuple[ModelRunStepSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_and_conditions_unique(self) -> ModelRunSpec:
        names = [step.name for step in self.steps]
        if len(set(names)) != len(names):
            raise ValueError("step names are unique within a run")
        conditions = [s.condition for s in self.steps if s.condition is not None]
        if len(set(conditions)) != len(conditions):
            raise ValueError("wait conditions are unique within a run")
        return self


class ModelRunStepRecord(BaseModel):
    """A step the engine recorded as complete, in recorded order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: EnumRunStepKind
    output: str = ""
    condition: str | None = None
    released_by: str | None = None


class ModelRunState(BaseModel):
    """A run as the engine records it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_key: str
    status: EnumRunStatus
    total_steps: int = 0
    recorded: tuple[ModelRunStepRecord, ...] = ()
    waiting_on: str | None = None
    detail: str = ""


class ModelRunEngineRequest(BaseModel):
    """One operation on the engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumRunEngineOperation
    run: ModelRunSpec | None = None
    run_key: str | None = Field(default=None, pattern=RUN_KEY_PATTERN)
    condition: str | None = Field(default=None, pattern=CONDITION_PATTERN)
    release_id: str | None = Field(default=None, pattern=RELEASE_ID_PATTERN)

    @model_validator(mode="after")
    def _fields_match_operation(self) -> ModelRunEngineRequest:
        op = self.operation
        if op is EnumRunEngineOperation.START_RUN:
            if self.run is None:
                raise ValueError("start_run carries a run")
        elif op is EnumRunEngineOperation.HEALTH:
            pass
        else:
            if self.run_key is None:
                raise ValueError(f"{op.value} names a run_key")
            if op is EnumRunEngineOperation.RELEASE_RUN and (
                self.condition is None or self.release_id is None
            ):
                raise ValueError("release_run names a condition and a release_id")
        return self


class ModelRunEngineResult(BaseModel):
    """The result of one operation. Only the field of the operation run is set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumRunEngineOperation
    backend: str
    start_outcome: EnumStartOutcome | None = None
    release_outcome: EnumReleaseOutcome | None = None
    cancel_outcome: EnumCancelOutcome | None = None
    state: ModelRunState | None = None
    healthy: bool | None = None
    detail: str = ""


class ModelTemporalSelfHostedSettings(BaseModel):
    """The deployment facts of the self-hosted Temporal backend. Supplied by the overlay; no value has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_host: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    task_queue: str = Field(min_length=1)
    command_start_to_close_seconds: int = Field(gt=0)
    heartbeat_timeout_seconds: int = Field(gt=0)
    heartbeat_interval_seconds: float = Field(gt=0)
    retry_initial_seconds: int = Field(gt=0)
    retry_max_seconds: int = Field(gt=0)
    retry_max_attempts: int = Field(gt=0)
    connect_timeout_seconds: int = Field(gt=0)


class EnumRunEngineBackend(StrEnum):
    """The overlay values of ``run_engine_backend``."""

    IN_MEMORY = "in_memory"
    TEMPORAL_SELF_HOSTED = "temporal_self_hosted"


class ModelRunEngineOverlay(BaseModel):
    """The node's overlay. Absent means the in-memory backend; the real backend needs its settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_engine_backend: EnumRunEngineBackend = EnumRunEngineBackend.IN_MEMORY
    temporal_self_hosted: ModelTemporalSelfHostedSettings | None = None

    @model_validator(mode="after")
    def _real_backend_has_settings(self) -> ModelRunEngineOverlay:
        wants = self.run_engine_backend is EnumRunEngineBackend.TEMPORAL_SELF_HOSTED
        if wants and self.temporal_self_hosted is None:
            raise ValueError("temporal_self_hosted needs its settings")
        if not wants and self.temporal_self_hosted is not None:
            raise ValueError("settings for a backend that is not selected")
        return self
