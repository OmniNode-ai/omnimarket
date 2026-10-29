# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Types of the WIP gate: readings, limits, exemptions, override evidence, decisions.

Shared by the WIP gate reducer node, the rule module ``rules_wip_gate`` and any
caller that asks the gate in process (the fill, the brief gate). Nothing here
names a repository, a host or an organization: the names arrive as data.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnumWipOutcome(StrEnum):
    """The three answers the gate gives a PR-opening dispatch."""

    ADMIT = "admit"
    WARN = "warn"
    REFUSE = "refuse"


class EnumWipReason(StrEnum):
    """Why the gate answered as it did."""

    UNDER_LIMIT = "under_limit"
    AT_LIMIT = "at_limit"
    EXEMPT = "exempt"
    OVERRIDE = "override"
    MISSING_READING = "missing_reading"
    UNREADABLE_STATE = "unreadable_state"
    STALE_READING = "stale_reading"
    LIMIT_MISSING = "limit_missing"


class EnumWipEvidenceWriter(StrEnum):
    """Who may write a piece of evidence; anything else is a forgery."""

    SLOT_REDUCER = "slot_reducer"
    DISPATCHER = "dispatcher"


class EnumWipExemptionKind(StrEnum):
    SWARM_LEASE = "swarm_lease"
    OPEN_PR = "open_pr"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelWipStateOutcome(_Frozen):
    """What a terminal state of the gate's machine means: its outcome and, when fixed, its reason."""

    outcome: EnumWipOutcome
    reason: EnumWipReason | None = None


class ModelWipGateConfig(_Frozen):
    """The ``config.wip_gate`` block of the reducer contract."""

    limit_floor: int = Field(ge=0)
    hours_of_landing: float = Field(gt=0)
    trailing_days: int = Field(gt=0)
    reading_max_age_minutes: float = Field(gt=0)
    override_evidence_minutes: float = Field(gt=0)
    state_outcomes: dict[str, ModelWipStateOutcome] = Field(min_length=1)


class ModelWipLimit(_Frozen):
    """One repository's limit with the measurement behind it."""

    limit: int = Field(ge=0)
    merges_per_hour: float = Field(ge=0)
    method: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class ModelWipPolicy(_Frozen):
    """What the gate reads from the deployment overlay."""

    enforce_ruling: str
    dispatchable_repos: tuple[str, ...]
    limits: dict[str, ModelWipLimit]
    exempt_roles: tuple[str, ...]

    @property
    def enforcing(self) -> bool:
        return bool(self.enforce_ruling.strip())


class ModelWipReading(_Frozen):
    """One repository's work in progress, read from the PR watcher state."""

    repo: str
    readable: bool
    read_at: datetime | None = None
    open: int = Field(default=0, ge=0)
    drafts: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)
    open_pr_numbers: tuple[int, ...] = ()
    unusable_reason: EnumWipReason | None = None


class ModelWipExemption(_Frozen):
    """A claim that a dispatch reduces work in progress, with its evidence."""

    kind: EnumWipExemptionKind
    repo: str
    lane_id: str
    role: str
    written_by: EnumWipEvidenceWriter
    expires_at: datetime
    pr_number: int | None = None


class ModelWipOverrideEvidence(_Frozen):
    """What the dispatcher writes when the slot reducer assigns an override."""

    slot_id: str
    slot_generation: int
    evidence_key: str
    repo: str
    dispatch_id: str
    decision_id: str
    written_by: EnumWipEvidenceWriter
    written_at: datetime


class ModelWipGateRequest(_Frozen):
    """One PR-opening dispatch asking to enter a repository."""

    repo: str
    dispatch_id: str
    now: datetime
    state: dict[str, Any] | None
    policy: ModelWipPolicy
    config: ModelWipGateConfig
    exemption: ModelWipExemption | None = None
    authorized_swarm_lanes: tuple[str, ...] = ()
    override: ModelWipOverrideEvidence | None = None
    override_consumed_by: str | None = None


class ModelWipGateDecision(_Frozen):
    """The gate's answer: admit, warn or refuse, the machine state and the line."""

    outcome: EnumWipOutcome
    fsm_state: str
    reason: EnumWipReason
    repo: str
    open: int | None
    drafts: int | None
    limit: int | None
    line: str


class ModelWipReadRequest(_Frozen):
    """Facts for ``read_wip``: the watcher state, ``now`` and the exemptions to count."""

    now: datetime
    state: dict[str, Any] | None
    policy: ModelWipPolicy
    config: ModelWipGateConfig
    exemptions: tuple[ModelWipExemption, ...] = ()
    authorized_swarm_lanes: tuple[str, ...] = ()


class ModelWipReadResult(_Frozen):
    lines: tuple[str, ...]


class ModelWipClaimRow(_Frozen):
    """A ledger CLAIM row, reduced to what the audit reads."""

    lane: str
    ts: datetime
    repo: str
    opens_pr: bool
    wip_idle_override: bool = False
    exemption: ModelWipExemption | None = None


class ModelWipAuditRequest(_Frozen):
    """Facts for ``audit_admissions``: CLAIM rows, readings over time, the state."""

    claims: tuple[ModelWipClaimRow, ...]
    readings: tuple[ModelWipReading, ...]
    policy: ModelWipPolicy
    state: dict[str, Any] | None = None
    authorized_swarm_lanes: tuple[str, ...] = ()


class ModelWipAuditResult(_Frozen):
    breaches: int
    breach_lanes: tuple[str, ...]
    excluded_override: int
    excluded_exempt: int
    unaudited: int
    line: str
