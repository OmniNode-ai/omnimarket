# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for projection replay/idempotence check (OMN-12884).

Classification follows the Phase 7 taxonomy from
docs/tracking/2026-06-09-dev-stability-loop-closure-plan.md:
  - replay-proven: duplicate event arrived; projection dedupe held (one row).
  - runtime-observed: seen once on the bus; no replay attempted yet.
  - dashboard-rendered: confirmed visible through the projection API.
  - blocked: correlation cannot be classified due to missing evidence.
  - superseded: a later row for the same key replaced this correlation.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class EnumReplayStatus(StrEnum):
    """Replay/idempotence classification for a projection correlation."""

    REPLAY_PROVEN = "replay-proven"
    RUNTIME_OBSERVED = "runtime-observed"
    DASHBOARD_RENDERED = "dashboard-rendered"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


class ModelProjectionEvent(BaseModel):
    """One projection event occurrence for a correlation."""

    model_config = ConfigDict(frozen=True)

    correlation_id: str
    source_topic: str
    partition: int
    offset: int
    table: str

    @field_validator("correlation_id")
    @classmethod
    def _non_empty_correlation_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("correlation_id must not be empty")
        return v

    @field_validator("source_topic")
    @classmethod
    def _non_empty_topic(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source_topic must not be empty")
        return v

    @field_validator("table")
    @classmethod
    def _non_empty_table(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("table must not be empty")
        return v


class ModelReplayCheckRequest(BaseModel):
    """Request: a list of projection events to evaluate for replay/dedupe."""

    model_config = ConfigDict(frozen=True)

    events: tuple[ModelProjectionEvent, ...]

    @field_validator("events")
    @classmethod
    def _non_empty_events(
        cls, v: tuple[ModelProjectionEvent, ...]
    ) -> tuple[ModelProjectionEvent, ...]:
        if not v:
            raise ValueError("events must not be empty")
        return v


class ModelCorrelationReplayResult(BaseModel):
    """Replay classification result for a single correlation_id + table pair."""

    model_config = ConfigDict(frozen=True)

    correlation_id: str
    table: str
    status: EnumReplayStatus
    occurrence_count: int
    dedupe_held: bool
    detail: str


class ModelReplayCheckResult(BaseModel):
    """Full result from a replay/idempotence check run."""

    model_config = ConfigDict(frozen=True)

    status: str  # "clean" | "findings" | "error"
    total_correlations: int
    replay_proven: int
    runtime_observed: int
    blocked: int
    superseded: int
    findings: tuple[ModelCorrelationReplayResult, ...]

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)
