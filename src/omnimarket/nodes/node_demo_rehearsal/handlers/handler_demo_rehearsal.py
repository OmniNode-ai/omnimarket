# SPDX-License-Identifier: MIT
"""HandlerDemoRehearsal — executes the demo rehearsal pipeline.

Runs demo command envelope, captures runtime topology manifest, probes
projection row and dashboard API, records optional screenshots, and
writes rehearsal_bundle.json to docs/evidence/demo-readiness/<run_id>/.

Never modifies runtime state, restarts services, or mutates production data.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_demo_rehearsal.models.model_demo_readiness import (
    ModelRehearsalBundle,
)

logger = logging.getLogger(__name__)

_DEFAULT_EVIDENCE_SUBDIR = "docs/evidence/demo-readiness"


def _default_omni_home() -> str:
    return os.environ["OMNI_HOME"]


class ModelDemoRehearsalRequest(BaseModel):
    """Input model for the demo rehearsal handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(
        default_factory=lambda: (
            f"demo-readiness-{datetime.now(UTC).strftime('%Y-%m-%dT%H%M%S')}"
        ),
        description="Unique run identifier for correlation and evidence continuity.",
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
        description="If true, run rehearsal but do not write artifacts.",
    )


class ModelDemoRehearsalResult(BaseModel):
    """Output model for the demo rehearsal handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., description="The run ID for this rehearsal.")
    bundle_path: str = Field(..., description="Path to rehearsal_bundle.json.")
    overall_status: str = Field(..., description="GREEN, DEGRADED, or BROKEN.")
    failure_count: int = Field(..., description="Number of failure items.")
    rehearsal_bundle: ModelRehearsalBundle = Field(..., description="Full bundle.")
    dry_run: bool = Field(..., description="Whether this was a dry run.")


class HandlerDemoRehearsal:
    """Executes the demo rehearsal pipeline and captures evidence bundle.

    Usage::

        handler = HandlerDemoRehearsal()
        result = await handler.handle(ModelDemoRehearsalRequest(run_id="demo-2026-05-18"))
    """

    def _resolve_evidence_dir(self, request: ModelDemoRehearsalRequest) -> Path:
        if request.evidence_dir is not None:
            return Path(request.evidence_dir)
        return Path(request.omni_home) / _DEFAULT_EVIDENCE_SUBDIR / request.run_id

    async def _probe_topology(self) -> dict[str, Any]:
        """Capture runtime topology manifest. Non-fatal on failure."""
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
            logger.warning("Topology probe failed: %s", exc)
        return {}

    async def _probe_projection(self) -> dict[str, Any] | None:
        """Probe projection row from DB. Non-fatal on failure."""
        db_url = os.environ.get("DEMO_PROJECTION_DB_URL")
        if not db_url:
            return None
        try:
            import asyncpg

            conn = await asyncpg.connect(db_url)
            try:
                row = await conn.fetchrow(
                    "SELECT * FROM projection_session_outcome ORDER BY captured_at DESC LIMIT 1"
                )
                return dict(row) if row else None
            finally:
                await conn.close()
        except Exception as exc:
            logger.warning("Projection probe failed: %s", exc)
        return None

    async def _probe_dashboard_api(self) -> dict[str, Any] | None:
        """Probe dashboard API health endpoint. Non-fatal on failure."""
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
            logger.warning("Dashboard API probe failed: %s", exc)
        return None

    def _classify_status(
        self,
        topology: dict[str, Any],
        projection: dict[str, Any] | None,
        dashboard: dict[str, Any] | None,
        failures: list[dict[str, Any]],
    ) -> str:
        if failures:
            critical_failures = [f for f in failures if f.get("severity") == "critical"]
            if critical_failures or not topology:
                return "BROKEN"
            return "DEGRADED"
        return "GREEN"

    async def handle(
        self, request: ModelDemoRehearsalRequest
    ) -> ModelDemoRehearsalResult:
        """Execute the demo rehearsal pipeline."""
        evidence_dir = self._resolve_evidence_dir(request)
        rehearsal_id = str(uuid.uuid4())
        timestamp_utc = datetime.now(UTC)
        failures: list[dict[str, Any]] = []

        topology = await self._probe_topology()
        if not topology:
            failures.append(
                {
                    "dimension": "topology",
                    "severity": "critical",
                    "msg": "Topology unreachable",
                }
            )

        projection = await self._probe_projection()
        dashboard = await self._probe_dashboard_api()

        if dashboard is None:
            failures.append(
                {
                    "dimension": "dashboard",
                    "severity": "warning",
                    "msg": "Dashboard API unavailable",
                }
            )

        overall_status = self._classify_status(
            topology, projection, dashboard, failures
        )

        bundle = ModelRehearsalBundle(
            rehearsal_id=rehearsal_id,
            timestamp_utc=timestamp_utc,
            command_envelope={"run_id": request.run_id, "rehearsal_id": rehearsal_id},
            runtime_topology_manifest=topology,
            projection_row=projection,
            dashboard_api_response=dashboard,
            screenshot_path=None,
            overall_status=overall_status,
            failures=failures,
        )

        bundle_path = evidence_dir / "rehearsal_bundle.json"

        if not request.dry_run:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            bundle_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
            logger.info(
                "Demo rehearsal %r complete: status=%s failures=%d → %s",
                request.run_id,
                overall_status,
                len(failures),
                bundle_path,
            )
        else:
            logger.info(
                "Demo rehearsal %r dry-run: status=%s failures=%d (no artifact written)",
                request.run_id,
                overall_status,
                len(failures),
            )

        return ModelDemoRehearsalResult(
            run_id=request.run_id,
            bundle_path=str(bundle_path),
            overall_status=overall_status,
            failure_count=len(failures),
            rehearsal_bundle=bundle,
            dry_run=request.dry_run,
        )


__all__: list[str] = [
    "HandlerDemoRehearsal",
    "ModelDemoRehearsalRequest",
    "ModelDemoRehearsalResult",
]
