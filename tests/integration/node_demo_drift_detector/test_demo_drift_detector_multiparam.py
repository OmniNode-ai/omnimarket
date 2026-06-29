# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_demo_drift_detector [OMN-13684].

WS-5 Wave 10. Variant A (direct in-process handler call). The I/O boundary
(topology / projection / dashboard probes that hit httpx + asyncpg) is replaced
by a constructor-free subclass that overrides the three async ``_probe_*``
methods — the real classification + tally logic runs unchanged. NEVER
monkeypatches httpx/asyncpg.

Each case varies the *current* runtime state against a fixed proof-of-green
rehearsal bundle and asserts the typed ``ModelDemoDriftDetectResult`` counts and
the finding dimension/criticality. Negative control: an unreachable topology
(was GREEN in the bundle) must produce exactly one DEMO_BLOCKER finding.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.events.demo_readiness import (
    EnumDemoCriticality,
    EnumDemoRehearsalStatus,
    ModelRehearsalBundle,
)
from omnimarket.nodes.node_demo_drift_detector.handlers.handler_demo_drift_detector import (
    HandlerDemoDriftDetector,
    ModelDemoDriftDetectRequest,
)

_GREEN_TOPOLOGY = {"nodes": 7, "status": "GREEN"}
_GREEN_PROJECTION = {"session": "abc", "outcome": "pass"}
_GREEN_DASHBOARD = {"health": "ok"}


class _StubProbeDriftDetector(HandlerDemoDriftDetector):
    """Override the live I/O probes with injected synthetic current-state."""

    def __init__(
        self,
        *,
        current_topology: dict[str, Any],
        current_projection: dict[str, Any] | None,
        current_dashboard: dict[str, Any] | None,
    ) -> None:
        self._current_topology = current_topology
        self._current_projection = current_projection
        self._current_dashboard = current_dashboard

    async def _probe_current_topology(self) -> dict[str, Any]:
        return self._current_topology

    async def _probe_current_projection(self) -> dict[str, Any] | None:
        return self._current_projection

    async def _probe_current_dashboard(self) -> dict[str, Any] | None:
        return self._current_dashboard


def _write_green_bundle(tmp_path: Path) -> str:
    bundle = ModelRehearsalBundle(
        rehearsal_id="green-2026-06-27",
        timestamp_utc=datetime.now(tz=UTC),
        runtime_topology_manifest=_GREEN_TOPOLOGY,
        projection_row=_GREEN_PROJECTION,
        dashboard_api_response=_GREEN_DASHBOARD,
        overall_status=EnumDemoRehearsalStatus.GREEN,
    )
    path = tmp_path / "rehearsal_bundle.json"
    path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    return str(path)


# (case_id, current_topology, current_projection, current_dashboard, dry_run,
#  expected) where expected = {total, blockers, degraded, dim, crit | None}
CASES = [
    pytest.param(
        _GREEN_TOPOLOGY,
        _GREEN_PROJECTION,
        _GREEN_DASHBOARD,
        False,
        {"total": 0, "blockers": 0, "degraded": 0, "dim": None, "crit": None},
        id="no-drift-clean",
    ),
    pytest.param(
        # NEGATIVE CONTROL: topology unreachable where it was GREEN -> blocker.
        {},
        _GREEN_PROJECTION,
        _GREEN_DASHBOARD,
        True,
        {
            "total": 1,
            "blockers": 1,
            "degraded": 0,
            "dim": "topology",
            "crit": EnumDemoCriticality.DEMO_BLOCKER,
        },
        id="topology-unreachable-NEGATIVE",
    ),
    pytest.param(
        {"nodes": 3, "status": "PARTIAL"},
        _GREEN_PROJECTION,
        _GREEN_DASHBOARD,
        True,
        {
            "total": 1,
            "blockers": 0,
            "degraded": 1,
            "dim": "topology",
            "crit": EnumDemoCriticality.DEMO_DEGRADED,
        },
        id="topology-differs-degraded",
    ),
    pytest.param(
        _GREEN_TOPOLOGY,
        None,
        _GREEN_DASHBOARD,
        True,
        {
            "total": 1,
            "blockers": 0,
            "degraded": 1,
            "dim": "projection",
            "crit": EnumDemoCriticality.DEMO_DEGRADED,
        },
        id="projection-missing-degraded",
    ),
    pytest.param(
        _GREEN_TOPOLOGY,
        _GREEN_PROJECTION,
        {"health": "degraded"},
        True,
        {
            "total": 1,
            "blockers": 0,
            "degraded": 0,
            "dim": "dashboard",
            "crit": EnumDemoCriticality.COSMETIC,
        },
        id="dashboard-differs-cosmetic",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    (
        "current_topology",
        "current_projection",
        "current_dashboard",
        "dry_run",
        "expected",
    ),
    CASES,
)
async def test_demo_drift_detector_multiparam(
    tmp_path: Path,
    current_topology: dict[str, Any],
    current_projection: dict[str, Any] | None,
    current_dashboard: dict[str, Any] | None,
    dry_run: bool,
    expected: dict[str, Any],
) -> None:
    green_path = _write_green_bundle(tmp_path)
    evidence_dir = tmp_path / "out"

    handler = _StubProbeDriftDetector(
        current_topology=current_topology,
        current_projection=current_projection,
        current_dashboard=current_dashboard,
    )
    result = await handler.handle(
        ModelDemoDriftDetectRequest(
            run_id="drift-2026-06-27",
            proof_of_green_path=green_path,
            evidence_dir=str(evidence_dir),
            dry_run=dry_run,
        )
    )

    assert result.total_finding_count == expected["total"]
    assert result.demo_blocker_count == expected["blockers"]
    assert result.demo_degraded_count == expected["degraded"]
    assert result.drift_report.proof_of_green_rehearsal_id == "green-2026-06-27"
    # tally counts must equal the actual findings list length (structural truth)
    assert len(result.drift_report.findings) == expected["total"]

    if expected["dim"] is not None:
        assert len(result.drift_report.findings) == 1
        finding = result.drift_report.findings[0]
        assert finding.dimension == expected["dim"]
        assert finding.criticality == expected["crit"]

    # dry_run controls artifact persistence: write iff not dry_run
    report_written = Path(result.report_path).exists()
    assert report_written is (not dry_run)
