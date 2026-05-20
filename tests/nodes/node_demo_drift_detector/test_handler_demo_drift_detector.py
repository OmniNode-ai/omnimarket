# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDemoDriftDetector.

Probes are patched. Verifies finding classification, criticality assignment,
dry_run behaviour, and artifact writing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from omnimarket.nodes.node_demo_drift_detector.handlers.handler_demo_drift_detector import (
    HandlerDemoDriftDetector,
    ModelDemoDriftDetectRequest,
)
from omnimarket.nodes.node_demo_rehearsal.models.model_demo_readiness import (
    EnumDemoCriticality,
    ModelRehearsalBundle,
)


def _write_green_bundle(
    tmp_path: Path,
    topology: dict,
    dashboard: dict | None,
    projection: dict | None = None,
) -> Path:
    from datetime import UTC, datetime

    bundle = ModelRehearsalBundle(
        rehearsal_id="green-001",
        timestamp_utc=datetime.now(UTC),
        runtime_topology_manifest=topology,
        projection_row=projection,
        dashboard_api_response=dashboard,
        overall_status="GREEN",
        failures=[],
    )
    bundle_dir = tmp_path / "green"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / "rehearsal_bundle.json"
    bundle_path.write_text(bundle.model_dump_json(), encoding="utf-8")
    return bundle_path


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_drift_when_identical(tmp_path: Path) -> None:
    topology = {"nodes": 3}
    dashboard = {"status": "ok"}
    green_path = _write_green_bundle(tmp_path, topology, dashboard)

    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value=topology)
        ),
        patch.object(
            handler, "_probe_current_dashboard", new=AsyncMock(return_value=dashboard)
        ),
        patch.object(
            handler, "_probe_current_projection", new=AsyncMock(return_value=None)
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="drift-test-clean",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=True,
            )
        )

    assert result.total_finding_count == 0
    assert result.demo_blocker_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_demo_blocker_when_topology_gone(tmp_path: Path) -> None:
    green_path = _write_green_bundle(tmp_path, {"nodes": 3}, {"status": "ok"})

    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value={})
        ),
        patch.object(
            handler,
            "_probe_current_dashboard",
            new=AsyncMock(return_value={"status": "ok"}),
        ),
        patch.object(
            handler, "_probe_current_projection", new=AsyncMock(return_value=None)
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="drift-test-blocker",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=True,
            )
        )

    assert result.demo_blocker_count >= 1
    blockers = [
        f
        for f in result.drift_report.findings
        if f.criticality == EnumDemoCriticality.DEMO_BLOCKER
    ]
    assert len(blockers) >= 1
    assert not blockers[0].auto_fixable


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cosmetic_when_dashboard_differs(tmp_path: Path) -> None:
    green_path = _write_green_bundle(tmp_path, {"nodes": 1}, {"status": "ok", "v": 1})

    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value={"nodes": 1})
        ),
        patch.object(
            handler,
            "_probe_current_dashboard",
            new=AsyncMock(return_value={"status": "ok", "v": 2}),
        ),
        patch.object(
            handler, "_probe_current_projection", new=AsyncMock(return_value=None)
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="drift-test-cosmetic",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=True,
            )
        )

    cosmetic = [
        f
        for f in result.drift_report.findings
        if f.criticality == EnumDemoCriticality.COSMETIC
    ]
    assert len(cosmetic) >= 1
    assert cosmetic[0].auto_fixable is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_projection_drift_is_reported(tmp_path: Path) -> None:
    green_path = _write_green_bundle(
        tmp_path,
        {"nodes": 1},
        {"status": "ok"},
        projection={"id": "green"},
    )

    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value={"nodes": 1})
        ),
        patch.object(
            handler,
            "_probe_current_dashboard",
            new=AsyncMock(return_value={"status": "ok"}),
        ),
        patch.object(
            handler,
            "_probe_current_projection",
            new=AsyncMock(return_value={"id": "current"}),
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="drift-test-projection",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=True,
            )
        )

    projection_findings = [
        f for f in result.drift_report.findings if f.dimension == "projection"
    ]
    assert len(projection_findings) == 1
    assert projection_findings[0].criticality == EnumDemoCriticality.DEMO_DEGRADED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drift_report_written(tmp_path: Path) -> None:
    green_path = _write_green_bundle(tmp_path, {"nodes": 2}, {"status": "ok"})

    handler = HandlerDemoDriftDetector()
    with (
        patch.object(
            handler, "_probe_current_topology", new=AsyncMock(return_value={})
        ),
        patch.object(
            handler,
            "_probe_current_dashboard",
            new=AsyncMock(return_value={"status": "ok"}),
        ),
        patch.object(
            handler, "_probe_current_projection", new=AsyncMock(return_value=None)
        ),
    ):
        result = await handler.handle(
            ModelDemoDriftDetectRequest(
                run_id="drift-test-write",
                proof_of_green_path=str(green_path),
                omni_home=str(tmp_path),
                dry_run=False,
            )
        )

    report_path = Path(result.report_path)
    assert report_path.exists()
    data = json.loads(report_path.read_text())
    assert data["run_id"] == "drift-test-write"
    assert isinstance(data["findings"], list)
