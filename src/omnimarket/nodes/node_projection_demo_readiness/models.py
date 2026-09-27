# SPDX-License-Identifier: MIT
"""Contract-owned row and ordering models for demo readiness."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.events.demo_readiness import EnumDemoDashboardConfiguration

type DemoNodeId = Literal["demo_rehearsal", "demo_drift_detector"]


class EnumDemoReadinessStatus(StrEnum):
    """A durable verdict, distinct from the source node's probe details."""

    GREEN = "GREEN"
    DEGRADED = "DEGRADED"
    BROKEN = "BROKEN"
    UNCONFIGURED = "UNCONFIGURED"
    DRY_RUN = "DRY_RUN"


class ModelDemoReadinessRow(BaseModel):
    """One latest accepted terminal observation per demo node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: DemoNodeId
    run_id: str = Field(min_length=1)
    status: EnumDemoReadinessStatus
    dashboard_configuration: EnumDemoDashboardConfiguration
    observed_at: datetime
    source_event_id: UUID
    evidence_path: str | None
    dry_run: bool
    failure_count: int | None = Field(ge=0)
    demo_blocker_count: int | None = Field(ge=0)
    demo_degraded_count: int | None = Field(ge=0)
    total_finding_count: int | None = Field(ge=0)
    projection_cursor: int | None = Field(default=None, ge=0)

    @field_validator("observed_at")
    @classmethod
    def require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_node_specific_shape(self) -> ModelDemoReadinessRow:
        if self.node_id == "demo_rehearsal":
            if self.failure_count is None or any(
                count is not None
                for count in (
                    self.demo_blocker_count,
                    self.demo_degraded_count,
                    self.total_finding_count,
                )
            ):
                raise ValueError("rehearsal row has invalid count shape")
        elif self.failure_count is not None or any(
            count is None
            for count in (
                self.demo_blocker_count,
                self.demo_degraded_count,
                self.total_finding_count,
            )
        ):
            raise ValueError("drift row has invalid count shape")
        if self.dry_run and self.evidence_path is not None:
            raise ValueError("dry-run row cannot claim durable evidence")
        if not self.dry_run and not self.evidence_path:
            raise ValueError("non-dry row requires an evidence path")
        if (
            self.dashboard_configuration is EnumDemoDashboardConfiguration.UNCONFIGURED
            and self.status is not EnumDemoReadinessStatus.UNCONFIGURED
        ):
            raise ValueError("unconfigured dashboard cannot have a different status")
        if self.dashboard_configuration is EnumDemoDashboardConfiguration.CONFIGURED:
            if self.status is EnumDemoReadinessStatus.UNCONFIGURED:
                raise ValueError("configured dashboard cannot have unconfigured status")
            if self.dry_run and self.status is not EnumDemoReadinessStatus.DRY_RUN:
                raise ValueError("configured dry-run cannot claim durable readiness")
            if not self.dry_run and self.status is EnumDemoReadinessStatus.DRY_RUN:
                raise ValueError("non-dry row cannot claim dry-run status")
        return self

    @property
    def ordering_key(self) -> tuple[datetime, UUID]:
        """Producer event time, then stable envelope ID; never arrival time."""
        return self.observed_at, self.source_event_id


class ModelDemoReadinessProjectionRequest(BaseModel):
    """Every input to the pure latest-row fold is explicit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    previous_row: ModelDemoReadinessRow | None
    observation: ModelDemoReadinessRow


class ModelDemoReadinessProjectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelDemoReadinessRow
    applied: bool
