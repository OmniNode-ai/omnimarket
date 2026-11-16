# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed counts in and a typed delegation-health verdict out (OMN-21927)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumDelegationHealthVerdict(StrEnum):
    """The four verdicts of the delegation-health check; the exit code is the contract's own."""

    OK = "OK"
    DEGRADED = "DEGRADED"
    UNREADABLE = "UNREADABLE"
    UNTRUSTED = "UNTRUSTED"


class EnumDelegationHealthFindingKind(StrEnum):
    """What a DEGRADED finding is about."""

    EXPECTED_TIER_UNRESOLVED = "expected_tier_unresolved"
    PIN_ONLY_RUNG = "pin_only_rung"
    HARNESS_RUNG = "harness_rung"
    TIMEOUTS_DOMINATE = "timeouts_dominate"
    EXPECTED_RUNG_SILENT = "expected_rung_silent"
    LOCAL_SHARE_ZERO = "local_share_zero"


class EnumExpectedRungRule(StrEnum):
    """Which count judges an expected rung."""

    BACKEND_ROW_THEN_MODEL = "backend_row_then_model"
    MODEL_COUNT = "model_count"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelWindowCounts(_Frozen):
    """Events in the last hour, in the quiet window and in the last 24 hours."""

    h1: int = Field(default=0, ge=0)
    window: int = Field(default=0, ge=0)
    h24: int = Field(default=0, ge=0)


class ModelModelCount(_Frozen):
    """Delegation events that one model answered."""

    model: str = Field(min_length=1)
    counts: ModelWindowCounts = Field(default_factory=ModelWindowCounts)
    last: str = ""


class ModelBackendCount(_Frozen):
    """Attempts and accepted answers of one backend (from the events' attempt history)."""

    backend_id: str = Field(min_length=1)
    counts: ModelWindowCounts = Field(default_factory=ModelWindowCounts)
    accepted: int = Field(default=0, ge=0)
    last: str = ""
    last_accept: str = ""


class ModelRung(_Frozen):
    """A rung of the rendered routing: an expected rung, or a pin-only rung."""

    tier: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class ModelCauseCount(_Frozen):
    """Real runs that ended with one terminal_failure_cause."""

    cause: str = Field(min_length=1)
    counts: ModelWindowCounts = Field(default_factory=ModelWindowCounts)


class ModelOutcomeCounts(_Frozen):
    """Outcome counts of the real runs: every run, and the failed ones by terminal_failure_cause."""

    runs: ModelWindowCounts
    causes: tuple[ModelCauseCount, ...] = ()


class ModelLocalShareCounts(_Frozen):
    """Delegated runs over the local share window and how many a local backend answered (OMN-24980).

    ``local_endpoints_up`` names the local endpoints the routing overlay declares that answered a
    model-list call when the caller read; the share is judged only while it is non-empty.
    """

    window_minutes: int = Field(gt=0)
    delegated_runs: int = Field(ge=0)
    local_runs: int = Field(ge=0)
    local_endpoints_up: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _local_within_delegated(self) -> "ModelLocalShareCounts":
        if self.local_runs > self.delegated_runs:
            raise ValueError("local_runs must not exceed delegated_runs")
        return self


class ModelDelegationHealthRequest(_Frozen):
    """Counts read by the caller. Reads stay with the caller; this node judges them."""

    correlation_id: str = Field(min_length=1)
    quiet_minutes: int = Field(gt=0)
    total_rows: int = Field(ge=0)
    read_failure: str = ""
    models: tuple[ModelModelCount, ...] = ()
    backends: tuple[ModelBackendCount, ...] = ()
    expected_rungs: tuple[ModelRung, ...] = ()
    pin_only_rungs: tuple[ModelRung, ...] = ()
    unresolved_tiers: tuple[str, ...] = ()
    outcomes: ModelOutcomeCounts | None = None
    lanes_running: int = Field(default=0, ge=0)
    local_share: ModelLocalShareCounts | None = None

    @field_validator("correlation_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("correlation_id must not be blank")
        return value


class ModelDelegationHealthFinding(_Frozen):
    """One reason the check is DEGRADED and the one FIX that answers it."""

    kind: EnumDelegationHealthFindingKind
    reason: str
    fix: str


class ModelDelegationHealthVerdict(_Frozen):
    """The typed verdict: findings only when DEGRADED, what was not judged, and the evidence lines."""

    correlation_id: str
    verdict: EnumDelegationHealthVerdict
    exit_code: int
    findings: tuple[ModelDelegationHealthFinding, ...] = ()
    not_judged: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    summary: str


class ModelDelegationHealthRules(_Frozen):
    """The contract's verdict_rules: every value the verdict varies with."""

    timeout_failure_cause: str = Field(min_length=1)
    timeout_share_percent: int = Field(ge=0, le=100)
    timeout_min_events: int = Field(ge=1)
    local_share_window_minutes: int = Field(gt=0)
    local_share_min_runs: int = Field(ge=1)
    expected_rung_rule: EnumExpectedRungRule
    exit_codes: dict[EnumDelegationHealthVerdict, int]
    smoke_command: str = Field(min_length=1)
    fixes: dict[EnumDelegationHealthFindingKind, str]

    @field_validator("exit_codes")
    @classmethod
    def _every_verdict_has_an_exit_code(
        cls, value: dict[EnumDelegationHealthVerdict, int]
    ) -> dict[EnumDelegationHealthVerdict, int]:
        if set(value) != set(EnumDelegationHealthVerdict):
            raise ValueError("exit_codes must name every verdict")
        return value

    @field_validator("fixes")
    @classmethod
    def _every_finding_has_a_fix(
        cls, value: dict[EnumDelegationHealthFindingKind, str]
    ) -> dict[EnumDelegationHealthFindingKind, str]:
        if set(value) != set(EnumDelegationHealthFindingKind):
            raise ValueError("fixes must name every finding kind")
        return value
