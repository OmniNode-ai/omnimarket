# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command, phase request and terminal event of the morning friction sweep."""

from datetime import date as calendar_date
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from .model_friction_phase_results import (
    ModelFrictionAdjudication,
    ModelFrictionPrecheck,
    ModelFrictionPrecheckUnavailable,
    ModelFrictionPremiseAudit,
    ModelFrictionScan,
    ModelFrictionSource,
    ModelFrictionSynthesis,
)

SOURCE_LABELS = (
    "friction-source-linear",
    "friction-source-checkpoints",
    "friction-source-ci",
    "friction-source-guards",
)


class ModelMorningFrictionSweepRequest(BaseModel):
    """A scheduled or deliberately forced friction sweep."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    correlation_id: UUID
    emitted_at: datetime
    tenant_id: str
    date: str
    dry_run: bool = Field(default=False, alias="dryRun")
    force: bool = False
    lookback_hours: float = Field(default=24, alias="lookbackHours", gt=0)
    fences: list[str] = Field(default_factory=list)

    @field_validator("date")
    @classmethod
    def valid_day(cls, value: str) -> str:
        if calendar_date.fromisoformat(value).isoformat() != value:
            raise ValueError("date must be a real YYYY-MM-DD date")
        return value

    @property
    def window_start(self) -> str:
        return (calendar_date.fromisoformat(self.date) - timedelta(days=1)).isoformat()


class ModelFrictionPhaseRequest(BaseModel):
    label: str
    phase: str
    model: str
    effort: str
    prompt: str
    schema_definition: dict[str, JsonValue]


class ModelMorningFrictionSweepResult(BaseModel):
    """Delivered report, or the zero-expensive-agent short circuit."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    correlation_id: UUID
    tenant_id: str
    date: str
    dry_run: bool
    force: bool
    short_circuited: str | None
    precheck: ModelFrictionPrecheck | ModelFrictionPrecheckUnavailable
    precheck_agents: int
    report: str
    state: str
    expensive_agents_spawned: int | None = None
    premise_audit: ModelFrictionPremiseAudit | None = None
    commit: str | None = None
    window: str | None = None
    scan: ModelFrictionScan | None = None
    sources: list[ModelFrictionSource] | None = None
    sources_unknown: list[str] | None = None
    synthesis: ModelFrictionSynthesis | None = None
    root_causes: list[str] | None = None
    pickups: list[str] | None = None
    adjudication: ModelFrictionAdjudication | None = None
    agents: int | None = None
