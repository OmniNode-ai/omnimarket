# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Capture repair evidence is separate from the original quiet-host verdict."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.silent_failure_alarm import ModelQuietHostResult


class ModelHostCaptureRepair(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    state: Literal[
        "unconfigured",
        "unknown",
        "not-safe",
        "not-needed",
        "dry-run",
        "cooldown",
        "limited",
        "failed",
        "pending-events",
        "recovered",
    ]
    attempted_at: AwareDatetime | None = None
    event_at: AwareDatetime | None = None
    detail: str = ""


class ModelQuietHostRepairResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ModelQuietHostResult
    repairs: tuple[ModelHostCaptureRepair, ...] = ()
    warnings: tuple[str, ...] = ()


class ModelHostDrainerCommand(BaseModel):
    """Trusted host overlay command invoking the installed node on that host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(default=660, gt=0)
