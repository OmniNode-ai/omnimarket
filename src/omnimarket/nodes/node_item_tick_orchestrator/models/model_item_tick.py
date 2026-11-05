# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed overlay and wire models. Policy is declared in the node contract."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, RootModel


class ModelItemTickBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_path: Path
    request: dict[str, JsonValue] = Field(default_factory=dict)


class ModelItemTickPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_seconds: int = Field(gt=0)
    required_checks: tuple[str, ...]
    success_statuses: tuple[str, ...]
    cursor_stale_intervals: int = Field(gt=0)
    missing_start_intervals: int = Field(gt=0)
    check_timeout_seconds: float = Field(gt=0)
    runtime_timeout_seconds: int = Field(gt=0)
    monitoring_entry: dict[str, JsonValue]


class ModelItemTickConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["item-tick/v1"]
    bus: Literal["inmemory", "kafka"]
    lane: str | None
    host: str = Field(min_length=1)
    state_directory: Path
    interval_seconds: int = Field(gt=0)
    check_timeout_seconds: float = Field(gt=0)
    checks: dict[str, ModelItemTickBinding]


class ModelItemTickRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    config: ModelItemTickConfig
    now: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: UUID = Field(default_factory=uuid4)


class ModelItemTickCommand(RootModel[dict[str, JsonValue]]):
    """A child node's overlay-bound payload; its contract owns its schema."""


class ModelItemTickCursor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str
    host: str
    fire_id: str
    correlation_id: UUID
    completed_at: AwareDatetime
    status: Literal["success", "failure"]
    result: dict[str, JsonValue]


class ModelItemTickResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "failure", "locked"]
    fire_id: str
    cursors: tuple[ModelItemTickCursor, ...] = ()
    errors: dict[str, str] = Field(default_factory=dict)
    lines: tuple[str, ...] = ()
