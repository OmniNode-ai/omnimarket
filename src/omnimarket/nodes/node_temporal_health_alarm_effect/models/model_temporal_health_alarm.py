# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three facts the Temporal health alarm reads, its settings, its request and its result."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelTemporalHealthFacts(BaseModel):
    """One reading of the server and the worker's task queue."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: datetime
    server_answers: bool
    worker_polling: bool
    newest_poll_at: datetime | None = None
    detail: str = ""


class ModelTemporalHealthSettings(BaseModel):
    """The contract's ``config`` block: names, bounds and the owner line of an alarm row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alarm: str = "temporal-down"
    poll_stale_after_s: int = Field(default=600, gt=0)
    row_lane: str = "temporal-health-alarm"
    row_actor: str = "script:temporal-health-alarm"
    row_ticket: str = ""
    alarm_owner: str = ""
    alarm_owner_line: str = ""
    temporal_cli: str = "temporal"
    address: str = "127.0.0.1:7233"
    namespace: str = "default"
    task_queue: str = ""
    cli_timeout_s: float = Field(default=20.0, gt=0)


class ModelTemporalHealthAlarmRequest(BaseModel):
    """A tick: the facts read now, the host they were read on and the episode state directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: ModelTemporalHealthFacts
    host: str
    state_dir: Path


class ModelTemporalHealthAlarmResult(BaseModel):
    """What the tick found and which row, if any, it wrote."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["healthy", "unhealthy"]
    reasons: tuple[str, ...] = ()
    row_kind: Literal["none", "alarm", "recovery"] = "none"
    row: str = ""
    outcome: Literal["none", "appended", "unappended"] = "none"
    episode: str = ""
    error: str = ""
