# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment settings, readings and durable state for the job-log probe."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest

NODE_NAME = "node_github_job_log_probe_effect"


class ModelJobLogProbeSettings(ModelScheduleDeclaration):
    runtime_lane: str = Field(min_length=1)
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    finished_job_id: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0, le=55)
    ledger_timeout_seconds: float = Field(gt=0, le=15)
    consumer_host: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$")
    owner_lane: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def load_job_log_probe_settings() -> ModelJobLogProbeSettings:
    return load_structured_overlay(NODE_NAME, ModelJobLogProbeSettings)


class ModelJobLogProbeReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["log_returned", "no_terminal", "gateway_failed", "unreadable"]
    reason: str = Field(pattern=r"^[A-Za-z0-9_]+$")


class ModelJobLogProbeState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    last_scheduled_at: datetime | None = None
    last_read_at: datetime | None = None
    target: tuple[str, int] | None = None
    no_terminal_streak: int = Field(default=0, ge=0, le=2)
    pending: ModelWorkLedgerAppendRequest | None = None
