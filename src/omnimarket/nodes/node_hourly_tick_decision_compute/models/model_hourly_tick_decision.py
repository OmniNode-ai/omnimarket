# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the hourly tick decisions (OMN-20680)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from omnibase_core.types import JsonType
from pydantic import BaseModel, ConfigDict, Field, model_validator

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class EnumHourlyTickDecisionKind(StrEnum):
    PROBE = "probe"
    RECORD = "record"


class ModelTickState(BaseModel):
    """The checkpoint window and its counts; derived from file contents, never from the clock alone."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    now: str = Field(pattern=_TS)
    prev: str = Field(pattern=_TS)
    sweep_since: str = Field(validation_alias="sweepSince", pattern=_TS)
    rows: int = Field(ge=0)
    terminal: int = Field(ge=0)
    lanes: str
    ledger_total: int = Field(validation_alias="ledgerTotal", gt=0)
    tail: list[str]

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.terminal > self.rows:
            raise ValueError("hourly-tick: TERMINAL rows exceed window rows")
        return self


class ModelSweepResult(BaseModel):
    """What the Linear-comments sweep returned; every count is optional (a skipped sweep has none)."""

    model_config = ConfigDict(frozen=True, extra="allow")
    __pydantic_extra__: dict[str, JsonType] = Field(init=False)

    collected: int | None = Field(default=None, ge=0)
    needing: int | None = Field(default=None, ge=0)
    accepted: int | None = Field(default=None, ge=0)
    held: int | None = Field(default=None, ge=0)
    held_by_class: dict[str, int] | None = None
    linear_file: str | None = None


class ModelSweepRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    since: str
    facts: str
    task_type: str = "document"


class ModelHourlyTickDecisionRequest(BaseModel):
    """probe: file contents in, window state out. record: state and sweep in, completion out."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    kind: EnumHourlyTickDecisionKind
    now: str | None = Field(default=None, pattern=_TS)
    checkpoint_text: str | None = None
    sweep_pointer_text: str | None = None
    ledger_text: str | None = None
    tail_lines: int = Field(default=12, validation_alias="tailLines", gt=0)
    facts: str = ""
    task_type: str = Field(default="document", validation_alias="taskType")
    skip_sweep: bool = Field(default=False, validation_alias="skipSweep")
    state: ModelTickState | None = None
    sweep: ModelSweepResult | None = None
    run_id: str = Field(default="", validation_alias="runId")
    fire_id: str = Field(
        default="",
        validation_alias="fireId",
        pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*)?$",
    )

    @model_validator(mode="after")
    def _kind_needs_its_fields(self) -> Self:
        probe_fields = ("now", "checkpoint_text", "sweep_pointer_text", "ledger_text")
        if self.kind is EnumHourlyTickDecisionKind.PROBE:
            missing = [f for f in probe_fields if getattr(self, f) is None]
            if missing:
                raise ValueError(f"hourly-tick: probe requires {', '.join(missing)}")
            if self.state is not None or self.sweep is not None:
                raise ValueError("hourly-tick: probe does not take state or sweep")
        else:
            if self.state is None:
                raise ValueError("hourly-tick: record requires the prepared state")
            given = [f for f in probe_fields if getattr(self, f) is not None]
            if given:
                raise ValueError(
                    f"hourly-tick: record does not take {', '.join(given)}"
                )
        return self


class ModelHourlyTickDecisionResult(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    kind: EnumHourlyTickDecisionKind
    state: ModelTickState
    checkpoint_line: str
    facts_source: str | None = None
    sweep_request: ModelSweepRequest | None = None
    completion_line: str | None = None
    phase: Literal["prepared", "completed", "degraded"]
    degraded: bool = False
    held_class: str | None = Field(
        default=None, validation_alias="class", serialization_alias="class"
    )
    result_payload: dict[str, JsonType] | None = None
