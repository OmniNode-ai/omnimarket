# SPDX-License-Identifier: MIT
"""HandlerDemoDriftDetector — diffs current demo state vs a proof-of-green bundle.

Truth hierarchy (highest to lowest): topology > projection > dashboard > screenshots.
Each finding is classified with the demo-criticality rubric and written to
drift_report.json.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_demo_rehearsal.models.model_demo_readiness import (
    EnumDemoCriticality,
    ModelDriftFinding,
    ModelRehearsalBundle,
)

logger = logging.getLogger(__name__)

_DEFAULT_EVIDENCE_SUBDIR = "docs/evidence/demo-readiness"


def _default_omni_home() -> str:
    return os.environ["OMNI_HOME"]


class ModelDemoDriftDetectRequest(BaseModel):
    """Input model for the demo drift detector handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., description="Run identifier for correlation.")
    proof_of_green_path: str = Field(
        ..., description="Path to proof-of-green rehearsal_bundle.json."
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
    dry_run: bool = Field(
        default=False,
        description="If true, detect drift but do not write report artifact.",
    )


class ModelDemoDriftReport(BaseModel):
    """Full drift report produced by the drift detector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., description="Run ID for this detection pass.")
    detected_at: datetime = Field(..., description="UTC timestamp of detection.")
    proof_of_green_rehearsal_id: str = Field(
        ..., description="Rehearsal ID of the proof-of-green bundle."
    )
    findings: list[ModelDriftFinding] = Field(
        default_factory=list, description="All drift findings."
    )
    demo_blocker_count: int = Field(default=0)
    demo_degraded_count: int = Field(default=0)
    cosmetic_count: int = Field(default=0)
    observability_only_count: int = Field(default=0)
    backlog_only_count: int = Field(default=0)


class ModelDemoDriftDetectResult(BaseModel):
    """Output model for the demo drift detector handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., description="The run ID for this drift detection.")
    report_path: str = Field(..., description="Path to drift_report.json.")
    demo_blocker_count: int = Field(..., description="Number of DEMO_BLOCKER findings.")
    demo_degraded_count: int = Field(
        ..., description="Number of DEMO_DEGRADED findings."
    )
    total_finding_count: int = Field(..., description="Total number of drift findings.")
    drift_report: ModelDemoDriftReport = Field(..., description="Full drift report.")
    dry_run: bool = Field(..., description="Whether this was a dry run.")


class HandlerDemoDriftDetector:
    """Diffs current demo state vs a proof-of-green rehearsal bundle.

    Usage::

        handler = HandlerDemoDriftDetector()
        result = await handler.handle(ModelDemoDriftDetectRequest(
            run_id="demo-2026-05-18",
            proof_of_green_path="docs/evidence/demo-readiness/green-run/rehearsal_bundle.json",
        ))
    """

    def _resolve_evidence_dir(self, request: ModelDemoDriftDetectRequest) -> Path:
        if request.evidence_dir is not None:
            return Path(request.evidence_dir)
        return Path(request.omni_home) / _DEFAULT_EVIDENCE_SUBDIR / request.run_id

    def _load_proof_of_green(self, path: str) -> ModelRehearsalBundle:
        return ModelRehearsalBundle.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )

    async def _probe_current_topology(self) -> dict[str, Any]:
        try:
            dashboard_url = os.environ.get(
                "DEMO_DASHBOARD_URL", "http://localhost:3000"
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{dashboard_url}/api/topology")
                if resp.status_code == 200:
                    data: dict[str, Any] = resp.json()
                    return data
        except Exception as exc:
            logger.warning("Current topology probe failed: %s", exc)
        return {}

    async def _probe_current_dashboard(self) -> dict[str, Any] | None:
        try:
            dashboard_url = os.environ.get(
                "DEMO_DASHBOARD_URL", "http://localhost:3000"
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{dashboard_url}/api/health")
                if resp.status_code == 200:
                    health: dict[str, Any] = resp.json()
                    return health
        except Exception as exc:
            logger.warning("Current dashboard probe failed: %s", exc)
        return None

    def _classify_topology_drift(
        self,
        green: dict[str, Any],
        current: dict[str, Any],
    ) -> list[ModelDriftFinding]:
        findings: list[ModelDriftFinding] = []
        if not current and green:
            findings.append(
                ModelDriftFinding(
                    finding_id=str(uuid.uuid4()),
                    dimension="topology",
                    criticality=EnumDemoCriticality.DEMO_BLOCKER,
                    summary="Runtime topology unreachable (was GREEN in proof-of-green)",
                    detail="Topology probe returned empty response; service may be down.",
                    auto_fixable=False,
                )
            )
        elif current != green and green:
            findings.append(
                ModelDriftFinding(
                    finding_id=str(uuid.uuid4()),
                    dimension="topology",
                    criticality=EnumDemoCriticality.DEMO_DEGRADED,
                    summary="Runtime topology manifest differs from proof-of-green",
                    detail=f"Green: {json.dumps(green, default=str)[:500]} | Current: {json.dumps(current, default=str)[:500]}",
                    auto_fixable=False,
                    fix_hint="Manual topology review required",
                )
            )
        return findings

    def _classify_dashboard_drift(
        self,
        green: dict[str, Any] | None,
        current: dict[str, Any] | None,
    ) -> list[ModelDriftFinding]:
        findings: list[ModelDriftFinding] = []
        if green is not None and current is None:
            findings.append(
                ModelDriftFinding(
                    finding_id=str(uuid.uuid4()),
                    dimension="dashboard",
                    criticality=EnumDemoCriticality.DEMO_DEGRADED,
                    summary="Dashboard API unavailable (was reachable in proof-of-green)",
                    detail="Dashboard health probe returned no response.",
                    auto_fixable=False,
                )
            )
        elif green is not None and current is not None and current != green:
            findings.append(
                ModelDriftFinding(
                    finding_id=str(uuid.uuid4()),
                    dimension="dashboard",
                    criticality=EnumDemoCriticality.COSMETIC,
                    summary="Dashboard API response differs from proof-of-green",
                    detail="Response shape changed; may be cosmetic version bump.",
                    auto_fixable=True,
                    fix_hint="Review dashboard API changelog for breaking changes",
                )
            )
        return findings

    def _tally_findings(self, findings: list[ModelDriftFinding]) -> dict[str, int]:
        tally: dict[str, int] = {c.value: 0 for c in EnumDemoCriticality}
        for f in findings:
            tally[f.criticality.value] += 1
        return tally

    async def handle(
        self, request: ModelDemoDriftDetectRequest
    ) -> ModelDemoDriftDetectResult:
        """Detect drift between current state and proof-of-green bundle."""
        evidence_dir = self._resolve_evidence_dir(request)
        detected_at = datetime.now(UTC)

        green_bundle = self._load_proof_of_green(request.proof_of_green_path)

        current_topology = await self._probe_current_topology()
        current_dashboard = await self._probe_current_dashboard()

        findings: list[ModelDriftFinding] = []
        findings.extend(
            self._classify_topology_drift(
                green_bundle.runtime_topology_manifest, current_topology
            )
        )
        findings.extend(
            self._classify_dashboard_drift(
                green_bundle.dashboard_api_response, current_dashboard
            )
        )

        tally = self._tally_findings(findings)

        report = ModelDemoDriftReport(
            run_id=request.run_id,
            detected_at=detected_at,
            proof_of_green_rehearsal_id=green_bundle.rehearsal_id,
            findings=findings,
            demo_blocker_count=tally[EnumDemoCriticality.DEMO_BLOCKER],
            demo_degraded_count=tally[EnumDemoCriticality.DEMO_DEGRADED],
            cosmetic_count=tally[EnumDemoCriticality.COSMETIC],
            observability_only_count=tally[EnumDemoCriticality.OBSERVABILITY_ONLY],
            backlog_only_count=tally[EnumDemoCriticality.BACKLOG_ONLY],
        )

        report_path = evidence_dir / "drift_report.json"

        if not request.dry_run:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            logger.info(
                "Demo drift detection %r complete: blockers=%d degraded=%d total=%d → %s",
                request.run_id,
                report.demo_blocker_count,
                report.demo_degraded_count,
                len(findings),
                report_path,
            )
        else:
            logger.info(
                "Demo drift detection %r dry-run: blockers=%d degraded=%d total=%d",
                request.run_id,
                report.demo_blocker_count,
                report.demo_degraded_count,
                len(findings),
            )

        return ModelDemoDriftDetectResult(
            run_id=request.run_id,
            report_path=str(report_path),
            demo_blocker_count=report.demo_blocker_count,
            demo_degraded_count=report.demo_degraded_count,
            total_finding_count=len(findings),
            drift_report=report,
            dry_run=request.dry_run,
        )


__all__: list[str] = [
    "HandlerDemoDriftDetector",
    "ModelDemoDriftDetectRequest",
    "ModelDemoDriftDetectResult",
    "ModelDemoDriftReport",
]
