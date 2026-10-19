# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs, checkpoint state and sweep boundary for the hourly tick."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid4

from omnibase_core.types import JsonType
from pydantic import BaseModel, ConfigDict, Field, model_validator

Timestamp = str


class ModelTickState(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")
    now: Timestamp = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    prev: Timestamp = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    sweep_since: Timestamp = Field(
        validation_alias="sweepSince", pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
    )
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
    model_config = ConfigDict(frozen=True, extra="allow")
    __pydantic_extra__: dict[str, JsonType] = Field(init=False)
    collected: int | None = Field(default=None, ge=0)
    needing: int | None = Field(default=None, ge=0)
    accepted: int | None = Field(default=None, ge=0)
    held: int | None = Field(default=None, ge=0)
    held_by_class: dict[str, int] | None = None
    linear_file: str | None = None
    failed: bool = False
    error_class: str | None = None


class ModelSweepRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    since: Timestamp
    facts: str
    task_type: str = "document"


class ModelHourlyTickRequest(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")
    operation: Literal["prepare", "record"] = "prepare"
    correlation_id: UUID = Field(default_factory=uuid4)
    registry_root: Path | None = None
    checkpoint_file: str = Field(
        default=".claude_scratch/checkpoints/2026-09-04-usage-window-checkpoint.md",
        validation_alias="checkpointFile",
    )
    sweep_state_file: str = Field(
        default=".claude_scratch/checkpoints/linear-sweep-last.txt",
        validation_alias="sweepStateFile",
    )
    ledger_file: str = Field(
        default="docs/tracking/ROLLING_WORK_LEDGER.md", validation_alias="ledgerFile"
    )
    now: Timestamp | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
    )
    tail_lines: int = Field(default=12, validation_alias="tailLines", gt=0)
    dry_run: bool = Field(default=False, validation_alias="dryRun")
    skip_sweep: bool = Field(default=False, validation_alias="skipSweep")
    post: bool = False
    run_id: str = Field(default="", validation_alias="runId")
    fire_id: str = Field(
        default="",
        validation_alias="fireId",
        pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*)?$",
    )
    facts: str = ""
    task_type: str = Field(default="document", validation_alias="taskType")
    state: ModelTickState | None = None
    sweep: ModelSweepResult | None = None

    @model_validator(mode="after")
    def validate_operation(self) -> Self:
        if self.post:
            raise ValueError(
                "hourly-tick: post:true is refused; per-message operator approval required"
            )
        if self.operation == "record" and (self.state is None or self.sweep is None):
            raise ValueError(
                "hourly-tick: record requires the prepared state and sweep result"
            )
        for path in (self.checkpoint_file, self.sweep_state_file, self.ledger_file):
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError(
                    "hourly-tick: file paths must be relative to OMNI_HOME"
                )
        return self


class ModelHourlyTickResult(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    correlation_id: UUID
    state: ModelTickState
    checkpoint_line: str
    completion_line: str = ""
    window: dict[str, str]
    sweep_since: Timestamp
    ledger_rows: int
    terminal_rows: int
    terminal_lanes: str
    ledger_total_control: int
    facts_source: str
    dry_run: bool
    sweep_skipped: bool
    sweep_request: ModelSweepRequest | None = None
    sweep: ModelSweepResult | None = None
    phase: Literal["prepared", "completed", "degraded"]
    degraded: bool = False
    held_class: str | None = Field(
        default=None, validation_alias="class", serialization_alias="class"
    )
    posted: Literal[False] = False
    operator_decisions_handed_back: str = (
        "which drafted replies to send, every bracketed [OPERATOR: ...] open point "
        "in the sweep report, and any lane the TERMINAL roster shows as stalled"
    )
    readbacks: dict[str, str | None]


class ModelPreparedTick(BaseModel):
    """Node-owned state, checked at the record boundary before any effects."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    request: ModelHourlyTickRequest
    state: ModelTickState
    readbacks: dict[str, str | None]
