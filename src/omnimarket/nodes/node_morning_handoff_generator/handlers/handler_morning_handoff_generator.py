# SPDX-License-Identifier: MIT
"""HandlerMorningHandoffGenerator — synthesizes overnight demo readiness evidence.

Reads rehearsal_bundle.json, drift_report.json, and fix_dispatch_log.json from
the evidence directory and produces:
  - human_summary: a paragraph suitable for a morning standup
  - morning_dispatch_plan.json: machine-readable plan with proposed fix waves

Supports replay mode: re-reads existing evidence without re-running any sweeps.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.demo_readiness import (
    EnumDemoCriticality,
    ModelDispatchIssue,
    ModelMorningDispatchPlan,
    ModelRehearsalBundle,
)
from omnimarket.nodes.node_demo_drift_detector.handlers.handler_demo_drift_detector import (
    ModelDemoDriftReport,
)
from omnimarket.nodes.node_demo_fix_dispatcher.handlers.handler_demo_fix_dispatcher import (
    ModelFixDispatchLog,
)

logger = logging.getLogger(__name__)

_DEFAULT_EVIDENCE_SUBDIR = "docs/evidence/demo-readiness"


def _default_omni_home() -> str:
    return os.environ["OMNI_HOME"]


class ModelMorningHandoffRequest(BaseModel):
    """Input model for the morning handoff generator handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., description="Run identifier for correlation.")
    evidence_dir: str | None = Field(
        default=None,
        description=(
            "Override evidence directory containing rehearsal_bundle.json, "
            "drift_report.json, fix_dispatch_log.json. "
            f"Defaults to {_DEFAULT_EVIDENCE_SUBDIR}/<run_id>/ relative to omni_home."
        ),
    )
    omni_home: str = Field(
        default_factory=_default_omni_home,
        description="Root path of the omni_home workspace.",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, generate plan but do not write morning_dispatch_plan.json.",
    )


class ModelMorningHandoffResult(BaseModel):
    """Output model for the morning handoff generator handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., description="The run ID for this handoff.")
    plan_path: str = Field(..., description="Path to morning_dispatch_plan.json.")
    human_summary: str = Field(..., description="Human-readable overnight summary.")
    demo_blocker_count: int = Field(
        ..., description="Number of unresolved DEMO_BLOCKER issues."
    )
    morning_dispatch_plan: ModelMorningDispatchPlan = Field(
        ..., description="Full machine-readable plan."
    )
    dry_run: bool = Field(..., description="Whether this was a dry run.")


class HandlerMorningHandoffGenerator:
    """Synthesizes overnight demo readiness evidence into a morning handoff plan.

    Usage::

        handler = HandlerMorningHandoffGenerator()
        result = await handler.handle(ModelMorningHandoffRequest(
            run_id="demo-2026-05-18",
        ))
    """

    def _resolve_evidence_dir(self, request: ModelMorningHandoffRequest) -> Path:
        if request.evidence_dir is not None:
            return Path(request.evidence_dir)
        return Path(request.omni_home) / _DEFAULT_EVIDENCE_SUBDIR / request.run_id

    def _load_rehearsal_bundle(self, evidence_dir: Path) -> ModelRehearsalBundle | None:
        path = evidence_dir / "rehearsal_bundle.json"
        if not path.exists():
            return None
        try:
            return ModelRehearsalBundle.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning("Failed to load rehearsal_bundle.json: %s", exc)
            return None

    def _load_drift_report(self, evidence_dir: Path) -> ModelDemoDriftReport | None:
        path = evidence_dir / "drift_report.json"
        if not path.exists():
            return None
        try:
            return ModelDemoDriftReport.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning("Failed to load drift_report.json: %s", exc)
            return None

    def _load_dispatch_log(self, evidence_dir: Path) -> ModelFixDispatchLog | None:
        path = evidence_dir / "fix_dispatch_log.json"
        if not path.exists():
            return None
        try:
            return ModelFixDispatchLog.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning("Failed to load fix_dispatch_log.json: %s", exc)
            return None

    def _build_human_summary(
        self,
        bundle: ModelRehearsalBundle | None,
        drift: ModelDemoDriftReport | None,
        dispatch: ModelFixDispatchLog | None,
    ) -> str:
        parts: list[str] = []

        if bundle is not None:
            parts.append(
                f"Demo rehearsal completed with status {bundle.overall_status} "
                f"({len(bundle.failures)} failure(s) recorded)."
            )
        else:
            parts.append("No rehearsal bundle found for this run.")

        if drift is not None:
            blocker_str = (
                f"{drift.demo_blocker_count} DEMO_BLOCKER"
                if drift.demo_blocker_count
                else "no blockers"
            )
            degraded_str = (
                f"{drift.demo_degraded_count} DEMO_DEGRADED"
                if drift.demo_degraded_count
                else "no degraded"
            )
            parts.append(
                f"Drift detection found {len(drift.findings)} finding(s): "
                f"{blocker_str}, {degraded_str}."
            )
        else:
            parts.append("No drift report found for this run.")

        if dispatch is not None:
            parts.append(
                f"Auto-fix dispatcher: {dispatch.fixes_dispatched} fix(es) dispatched, "
                f"{dispatch.fixes_skipped_human_approval} requiring human approval."
            )
        else:
            parts.append("No fix dispatch log found for this run.")

        return " ".join(parts)

    def _build_issues(
        self,
        drift: ModelDemoDriftReport | None,
        dispatch: ModelFixDispatchLog | None,
    ) -> list[ModelDispatchIssue]:
        if drift is None:
            return []

        dispatched_ids: set[str] = set()
        if dispatch is not None:
            dispatched_ids = {r.finding_id for r in dispatch.records if r.dispatched}

        issues: list[ModelDispatchIssue] = []
        for finding in drift.findings:
            issues.append(
                ModelDispatchIssue(
                    issue_id=finding.finding_id,
                    criticality=finding.criticality,
                    summary=finding.summary,
                    fix_dispatched=finding.finding_id in dispatched_ids,
                    requires_human=finding.criticality
                    in (
                        EnumDemoCriticality.DEMO_BLOCKER,
                        EnumDemoCriticality.DEMO_DEGRADED,
                    ),
                )
            )
        return issues

    def _build_dispatch_waves(
        self, issues: list[ModelDispatchIssue]
    ) -> list[dict[str, Any]]:
        """Group unresolved human-approval issues into proposed dispatch waves."""
        human_issues = [i for i in issues if i.requires_human and not i.fix_dispatched]
        if not human_issues:
            return []

        blockers = [
            i for i in human_issues if i.criticality == EnumDemoCriticality.DEMO_BLOCKER
        ]
        degraded = [
            i
            for i in human_issues
            if i.criticality == EnumDemoCriticality.DEMO_DEGRADED
        ]

        waves: list[dict[str, Any]] = []
        if blockers:
            waves.append(
                {
                    "wave": 1,
                    "priority": "critical",
                    "label": "DEMO_BLOCKER resolution",
                    "issue_ids": [i.issue_id for i in blockers],
                    "requires_human_approval": True,
                }
            )
        if degraded:
            waves.append(
                {
                    "wave": len(waves) + 1,
                    "priority": "high",
                    "label": "DEMO_DEGRADED resolution",
                    "issue_ids": [i.issue_id for i in degraded],
                    "requires_human_approval": True,
                }
            )
        return waves

    async def handle(
        self, request: ModelMorningHandoffRequest
    ) -> ModelMorningHandoffResult:
        """Synthesize overnight evidence into morning handoff plan."""
        evidence_dir = self._resolve_evidence_dir(request)
        generated_at = datetime.now(UTC)

        bundle = self._load_rehearsal_bundle(evidence_dir)
        drift = self._load_drift_report(evidence_dir)
        dispatch = self._load_dispatch_log(evidence_dir)

        human_summary = self._build_human_summary(bundle, drift, dispatch)
        issues = self._build_issues(drift, dispatch)
        waves = self._build_dispatch_waves(issues)

        plan = ModelMorningDispatchPlan(
            generated_at=generated_at,
            overnight_summary=human_summary,
            issues=issues,
            proposed_dispatch_waves=waves,
        )

        demo_blocker_count = sum(
            1
            for i in issues
            if i.criticality == EnumDemoCriticality.DEMO_BLOCKER
            and not i.fix_dispatched
        )

        plan_path = evidence_dir / "morning_dispatch_plan.json"

        if not request.dry_run:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
            logger.info(
                "Morning handoff %r generated: blockers=%d issues=%d waves=%d → %s",
                request.run_id,
                demo_blocker_count,
                len(issues),
                len(waves),
                plan_path,
            )
        else:
            logger.info(
                "Morning handoff %r dry-run: blockers=%d issues=%d waves=%d",
                request.run_id,
                demo_blocker_count,
                len(issues),
                len(waves),
            )

        return ModelMorningHandoffResult(
            run_id=request.run_id,
            plan_path=str(plan_path),
            human_summary=human_summary,
            demo_blocker_count=demo_blocker_count,
            morning_dispatch_plan=plan,
            dry_run=request.dry_run,
        )


__all__: list[str] = [
    "HandlerMorningHandoffGenerator",
    "ModelMorningHandoffRequest",
    "ModelMorningHandoffResult",
]
