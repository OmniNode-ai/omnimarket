# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Commands and terminal events for the morning operator workflow."""

from datetime import date as calendar_date
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

PHASE_ORDER = ("GroundState", "Triage", "Reconcile", "Integrate", "DroppedWork", "Goal")
UNCONDITIONAL_PHASES = ("DroppedWork", "Goal")
DROPPED_SECTIONS = (
    "RED ON DEV HEAD",
    "STALE PLANS",
    "UNSTARTED WORK BY AGE",
    "UNREAD LANE REPORTS",
    "PROPOSED DISPATCH LIST",
)
DROPPED_HEADLINE_KEYS = (
    "dropped_work",
    "red_on_dev_head",
    "stale_plans",
    "unstarted_work_by_age",
    "unread_lane_reports",
    "proposed_dispatch",
)


class ModelMorningGroundStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    correlation_id: UUID
    emitted_at: datetime
    tenant_id: str
    date: str
    fences: list[str] = Field(default_factory=list)
    publish: bool = True
    force: bool = False
    # None means "not supplied": the deployment overlay's request_defaults apply.
    plan_docs: list[str] | None = Field(default=None, alias="planDocs")
    must_do_doc: str | None = Field(default=None, alias="mustDoDoc")
    off_rails_doc: str | None = Field(default=None, alias="offRailsDoc")
    kb_internal_path: str = Field(default="", alias="kbInternalPath")
    integration_repos: list[str] | None = Field(default=None, alias="integrationRepos")
    integrate_since: str = Field(default="", alias="integrateSince")
    closure_probe_ticket: str | None = Field(default=None, alias="closureProbeTicket")
    closure_probe_ceiling_seconds: int = Field(
        default=600, alias="closureProbeCeilingSeconds", gt=0
    )
    live_lanes: list[str] | None = Field(default=None, alias="liveLanes")

    @field_validator("date")
    @classmethod
    def valid_day(cls, value: str) -> str:
        if calendar_date.fromisoformat(value).isoformat() != value:
            raise ValueError("date must be a real YYYY-MM-DD date")
        return value

    @property
    def window_start(self) -> str:
        return (
            self.integrate_since
            or (calendar_date.fromisoformat(self.date) - timedelta(days=1)).isoformat()
        )


class ModelMorningPhaseRequest(BaseModel):
    label: str
    phase: str
    model: str
    effort: str
    prompt: str
    schema_definition: dict[str, JsonValue]


class ModelMorningPhaseFailure(BaseModel):
    """Recorded cause when best-effort phase work could not complete."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    failure_type: str
    failure_reason: str


class ModelMorningGroundStateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correlation_id: UUID
    tenant_id: str
    date: str
    publish: bool
    force: bool
    window: str
    ledger_reconcile_failure: ModelMorningPhaseFailure | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    precheck: dict[str, JsonValue]
    expensive_agents_spawned: int
    unconditional_agents_spawned: int
    unconditional_phases: list[str]
    precheck_agents: int
    phases_run: list[str]
    phases_short_circuited: list[str]
    ground: dict[str, JsonValue] | None
    triage: dict[str, JsonValue] | None
    reconcile: dict[str, JsonValue] | None
    integrate: dict[str, JsonValue] | None
    dropped_work: dict[str, JsonValue] | None
    goal: dict[str, JsonValue] | None
