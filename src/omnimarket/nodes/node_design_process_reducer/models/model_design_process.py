# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts and events in, next state out of node_design_process_reducer (OMN-21460)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EnumReadbackStatus(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class ModelDesignProcessFacts(BaseModel):
    """The facts the guards read; the record checker computes the first four."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_issue_count: int = Field(default=0, ge=0)
    failure_mode_rows: int = Field(default=0, ge=0)
    rows_without_detector_or_gap: int = Field(default=0, ge=0)
    live_check_command_present: bool = False
    live_check_failing_reading: bool = False
    deploy_receipt_present: bool = False
    readback_status: EnumReadbackStatus | None = None


class ModelDesignProcessRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str | None = None
    trigger: str = Field(min_length=1)
    facts: ModelDesignProcessFacts = ModelDesignProcessFacts()
    now: AwareDatetime


class ModelDesignProcessResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prior_state: str
    state: str
    moved: bool
    error: str | None = None
    failed_conditions: tuple[str, ...] = ()
    decided_at: AwareDatetime
