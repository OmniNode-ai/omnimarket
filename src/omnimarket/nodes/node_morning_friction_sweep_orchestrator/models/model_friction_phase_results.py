# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed results of the friction sweep phases; no untyped agent payload crosses the handler."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Phase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelFrictionDelegation(_Phase):
    delegated: int
    runs: list[str]
    reason: str


class ModelFrictionPrecheck(_Phase):
    date: str
    clock_utc: str
    verdict: Literal["already-delivered", "peer-owned", "run"]
    evidence: str
    reason: str
    state_last_run_date: str | None = None
    checks_failed: list[str] | None = None


class ModelFrictionPrecheckUnavailable(_Phase):
    """Force or an unavailable precheck: the original bypass explanation."""

    bypassed: str
    failure_type: str | None = None
    failure_reason: str | None = None


class ModelFrictionScan(_Phase):
    handoff_path: str
    sources_read: list[str]
    candidate_count: float
    unreadable_sources: list[str]
    ledger_rows: list[str]


class ModelFrictionSource(_Phase):
    handoff_path: str
    source_id: str
    verdict: str
    freshness: str
    candidate_count: float
    unreadable: list[str]
    positive_controls: list[str] | None = None
    ledger_rows: list[str]


class ModelFrictionSynthesis(_Phase):
    handoff_path: str
    candidate_count: float
    root_causes: list[str]
    sources_unknown: list[str]
    pickups: list[str]
    ledger_rows: list[str]
    delegation: ModelFrictionDelegation


class ModelFrictionAdjudication(_Phase):
    handoff_path: str
    filed: list[str]
    commented: list[str]
    known: list[str]
    first_occurrence: list[str]
    escalated: list[str]
    ledger_rows: list[str]
    delegation: ModelFrictionDelegation


class ModelFrictionPositiveControl(_Phase):
    lane: str
    terminal_rows: int
    evidence: str


class ModelFrictionPremiseAudit(_Phase):
    status: Literal["MEASURED", "UNKNOWN"]
    rule_landed_at: str
    rule_evidence: str
    eligible_terminal_rows: int
    falsified_terminal_rows: int | None
    positive_controls: list[ModelFrictionPositiveControl]
    unreadable_sources: list[str]
    reason: str


class ModelFrictionReport(_Phase):
    report_path: str
    state_path: str
    commit_sha: str
    watermark_summary: str
    ledger_rows: list[str]
    premise_audit: ModelFrictionPremiseAudit
    delegation: ModelFrictionDelegation
