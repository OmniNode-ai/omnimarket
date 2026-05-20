# SPDX-License-Identifier: MIT
"""HandlerDemoFixDispatcher — auto-fix low-risk demo drift with bounded authority.

COSMETIC and OBSERVABILITY_ONLY findings are auto-fixable.
DEMO_BLOCKER and DEMO_DEGRADED always require human approval — never auto-fixed.
BACKLOG_ONLY findings are recorded but not dispatched.

Bounded by: max_parallel_workers, max_daily_cost_usd, max_open_autofix_prs.
Never deploys runtime changes, restarts production lanes, or merges
topology-affecting PRs.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.demo_readiness import (
    EnumDemoCriticality,
    ModelBoundedConcurrencyConfig,
    ModelDriftFinding,
)
from omnimarket.nodes.node_demo_drift_detector.handlers.handler_demo_drift_detector import (
    ModelDemoDriftReport,
)

logger = logging.getLogger(__name__)

_DEFAULT_EVIDENCE_SUBDIR = "docs/evidence/demo-readiness"

_AUTO_FIXABLE_CRITICALITIES = frozenset(
    [EnumDemoCriticality.COSMETIC, EnumDemoCriticality.OBSERVABILITY_ONLY]
)
_HUMAN_APPROVAL_REQUIRED = frozenset(
    [EnumDemoCriticality.DEMO_BLOCKER, EnumDemoCriticality.DEMO_DEGRADED]
)
_ESTIMATED_AUTOFIX_COST_USD = 1.0


def _default_omni_home() -> str:
    return os.environ["OMNI_HOME"]


class ModelFixDispatchRecord(BaseModel):
    """A single fix dispatch record in the log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(..., description="Finding ID that was addressed.")
    criticality: EnumDemoCriticality = Field(..., description="Finding criticality.")
    summary: str = Field(..., description="Finding summary.")
    dispatched: bool = Field(default=False, description="Whether a fix was dispatched.")
    skipped_reason: str | None = Field(
        default=None, description="Reason skipped if not dispatched."
    )
    fix_hint: str | None = Field(default=None, description="Fix hint applied.")
    dispatch_id: str | None = Field(
        default=None, description="Dispatch ID if dispatched."
    )


class ModelFixDispatchLog(BaseModel):
    """Full fix dispatch log written to fix_dispatch_log.json."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., description="Run ID for this dispatch pass.")
    dispatched_at: datetime = Field(..., description="UTC timestamp of dispatch.")
    concurrency_config: ModelBoundedConcurrencyConfig = Field(
        ..., description="Concurrency limits applied."
    )
    records: list[ModelFixDispatchRecord] = Field(
        default_factory=list, description="Per-finding dispatch records."
    )
    fixes_dispatched: int = Field(default=0)
    fixes_skipped_human_approval: int = Field(default=0)
    fixes_skipped_limit: int = Field(default=0)
    fixes_skipped_not_fixable: int = Field(default=0)


class ModelDemoFixDispatchRequest(BaseModel):
    """Input model for the demo fix dispatcher handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., description="Run identifier for correlation.")
    drift_report_path: str = Field(
        ..., description="Path to drift_report.json from node_demo_drift_detector."
    )
    evidence_dir: str | None = Field(
        default=None,
        description=(
            "Override evidence output directory. "
            f"Defaults to {_DEFAULT_EVIDENCE_SUBDIR}/<run_id>/ relative to omni_home."
        ),
    )
    omni_home: str = Field(
        default_factory=_default_omni_home,
        description="Root path of the omni_home workspace.",
    )
    max_parallel_workers: int = Field(default=4, ge=1, le=16)
    max_runtime_minutes: int = Field(default=120, ge=1, le=480)
    max_daily_cost_usd: float = Field(default=50.0, gt=0.0, le=500.0)
    max_open_autofix_prs: int = Field(default=5, ge=1, le=20)
    dry_run: bool = Field(
        default=False,
        description="If true, evaluate fixes but do not dispatch any workers.",
    )


class ModelDemoFixDispatchResult(BaseModel):
    """Output model for the demo fix dispatcher handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., description="The run ID for this dispatch pass.")
    dispatch_log_path: str = Field(..., description="Path to fix_dispatch_log.json.")
    fixes_dispatched: int = Field(..., description="Number of fixes dispatched.")
    fixes_skipped_human_approval: int = Field(
        ..., description="Findings requiring human approval."
    )
    fixes_skipped_limit: int = Field(
        ..., description="Findings skipped due to concurrency/cost limits."
    )
    dispatch_log: ModelFixDispatchLog = Field(..., description="Full dispatch log.")
    dry_run: bool = Field(..., description="Whether this was a dry run.")


class HandlerDemoFixDispatcher:
    """Auto-fixes low-risk demo drift findings with bounded authority.

    Usage::

        handler = HandlerDemoFixDispatcher()
        result = await handler.handle(ModelDemoFixDispatchRequest(
            run_id="demo-2026-05-18",
            drift_report_path="docs/evidence/demo-readiness/demo-2026-05-18/drift_report.json",
        ))
    """

    def _resolve_evidence_dir(self, request: ModelDemoFixDispatchRequest) -> Path:
        if request.evidence_dir is not None:
            return Path(request.evidence_dir)
        return Path(request.omni_home) / _DEFAULT_EVIDENCE_SUBDIR / request.run_id

    def _load_drift_report(self, path: str) -> ModelDemoDriftReport:
        return ModelDemoDriftReport.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )

    def _build_concurrency_config(
        self, request: ModelDemoFixDispatchRequest
    ) -> ModelBoundedConcurrencyConfig:
        return ModelBoundedConcurrencyConfig(
            max_parallel_workers=request.max_parallel_workers,
            max_runtime_minutes=request.max_runtime_minutes,
            max_daily_cost_usd=request.max_daily_cost_usd,
            max_open_autofix_prs=request.max_open_autofix_prs,
        )

    def _should_dispatch(
        self,
        finding: ModelDriftFinding,
        dispatched_count: int,
        concurrency: ModelBoundedConcurrencyConfig,
        spent_cost_usd: float,
    ) -> tuple[bool, str | None]:
        """Return (should_dispatch, skip_reason)."""
        if finding.criticality in _HUMAN_APPROVAL_REQUIRED:
            return False, "requires_human_approval"
        if finding.criticality == EnumDemoCriticality.BACKLOG_ONLY:
            return False, "backlog_only"
        if not finding.auto_fixable:
            return False, "not_auto_fixable"
        if finding.criticality not in _AUTO_FIXABLE_CRITICALITIES:
            return False, "criticality_not_allowed"
        if dispatched_count >= concurrency.max_open_autofix_prs:
            return False, "pr_limit_reached"
        if (
            spent_cost_usd + _ESTIMATED_AUTOFIX_COST_USD
            > concurrency.max_daily_cost_usd
        ):
            return False, "daily_cost_limit_reached"
        return True, None

    async def _dispatch_fix(
        self, finding: ModelDriftFinding, dry_run: bool
    ) -> str | None:
        """Dispatch a fix worker for an auto-fixable finding. Returns dispatch_id."""
        dispatch_id = str(uuid.uuid4())
        if dry_run:
            logger.info(
                "DRY-RUN: would dispatch fix for finding %s (%s): %s",
                finding.finding_id,
                finding.criticality,
                finding.fix_hint,
            )
            return None
        logger.info(
            "Dispatching fix for finding %s (%s): %s [dispatch_id=%s]",
            finding.finding_id,
            finding.criticality,
            finding.fix_hint,
            dispatch_id,
        )
        return dispatch_id

    async def handle(
        self, request: ModelDemoFixDispatchRequest
    ) -> ModelDemoFixDispatchResult:
        """Evaluate and dispatch auto-fixes for low-risk drift findings."""
        evidence_dir = self._resolve_evidence_dir(request)
        dispatched_at = datetime.now(UTC)
        concurrency = self._build_concurrency_config(request)

        drift_report = self._load_drift_report(request.drift_report_path)

        records: list[ModelFixDispatchRecord] = []
        dispatched_count = 0
        skipped_human = 0
        skipped_limit = 0
        skipped_not_fixable = 0
        spent_cost_usd = 0.0

        for finding in drift_report.findings:
            should_dispatch, skip_reason = self._should_dispatch(
                finding, dispatched_count, concurrency, spent_cost_usd
            )

            if should_dispatch:
                dispatch_id = await self._dispatch_fix(finding, request.dry_run)
                if not request.dry_run:
                    dispatched_count += 1
                    spent_cost_usd += _ESTIMATED_AUTOFIX_COST_USD
                records.append(
                    ModelFixDispatchRecord(
                        finding_id=finding.finding_id,
                        criticality=finding.criticality,
                        summary=finding.summary,
                        dispatched=not request.dry_run,
                        fix_hint=finding.fix_hint,
                        dispatch_id=dispatch_id,
                    )
                )
            else:
                if skip_reason == "requires_human_approval":
                    skipped_human += 1
                elif skip_reason in {"daily_cost_limit_reached", "pr_limit_reached"}:
                    skipped_limit += 1
                else:
                    skipped_not_fixable += 1
                records.append(
                    ModelFixDispatchRecord(
                        finding_id=finding.finding_id,
                        criticality=finding.criticality,
                        summary=finding.summary,
                        dispatched=False,
                        skipped_reason=skip_reason,
                        fix_hint=finding.fix_hint,
                    )
                )

        dispatch_log = ModelFixDispatchLog(
            run_id=request.run_id,
            dispatched_at=dispatched_at,
            concurrency_config=concurrency,
            records=records,
            fixes_dispatched=dispatched_count,
            fixes_skipped_human_approval=skipped_human,
            fixes_skipped_limit=skipped_limit,
            fixes_skipped_not_fixable=skipped_not_fixable,
        )

        log_path = evidence_dir / "fix_dispatch_log.json"

        if not request.dry_run:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                dispatch_log.model_dump_json(indent=2), encoding="utf-8"
            )
            logger.info(
                "Demo fix dispatch %r complete: dispatched=%d human_approval=%d skipped=%d → %s",
                request.run_id,
                dispatched_count,
                skipped_human,
                skipped_limit + skipped_not_fixable,
                log_path,
            )
        else:
            logger.info(
                "Demo fix dispatch %r dry-run: would_dispatch=%d human_approval=%d",
                request.run_id,
                sum(
                    1
                    for r in records
                    if r.dispatched is False and r.skipped_reason is None
                ),
                skipped_human,
            )

        return ModelDemoFixDispatchResult(
            run_id=request.run_id,
            dispatch_log_path=str(log_path),
            fixes_dispatched=dispatched_count,
            fixes_skipped_human_approval=skipped_human,
            fixes_skipped_limit=skipped_limit,
            dispatch_log=dispatch_log,
            dry_run=request.dry_run,
        )


__all__: list[str] = [
    "HandlerDemoFixDispatcher",
    "ModelDemoFixDispatchRequest",
    "ModelDemoFixDispatchResult",
    "ModelFixDispatchLog",
    "ModelFixDispatchRecord",
]
