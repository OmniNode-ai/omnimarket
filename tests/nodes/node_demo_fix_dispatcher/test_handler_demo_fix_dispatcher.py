# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDemoFixDispatcher.

Verifies bounded authority: DEMO_BLOCKER/DEMO_DEGRADED never auto-fixed,
COSMETIC auto-fixable, PR limit respected, dry_run works.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.events.demo_readiness import (
    EnumDemoCriticality,
    ModelDriftFinding,
)
from omnimarket.nodes.node_demo_drift_detector.handlers.handler_demo_drift_detector import (
    ModelDemoDriftReport,
)
from omnimarket.nodes.node_demo_fix_dispatcher.handlers.handler_demo_fix_dispatcher import (
    HandlerDemoFixDispatcher,
    ModelDemoFixDispatchRequest,
)


def _write_drift_report(tmp_path: Path, findings: list[ModelDriftFinding]) -> Path:
    report = ModelDemoDriftReport(
        run_id="test-run",
        detected_at=datetime.now(UTC),
        proof_of_green_rehearsal_id="green-001",
        findings=findings,
        demo_blocker_count=sum(
            1 for f in findings if f.criticality == EnumDemoCriticality.DEMO_BLOCKER
        ),
        demo_degraded_count=sum(
            1 for f in findings if f.criticality == EnumDemoCriticality.DEMO_DEGRADED
        ),
    )
    report_dir = tmp_path / "test-run"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "drift_report.json"
    report_path.write_text(report.model_dump_json(), encoding="utf-8")
    return report_path


def _finding(
    criticality: EnumDemoCriticality, auto_fixable: bool = True
) -> ModelDriftFinding:
    return ModelDriftFinding(
        finding_id=str(uuid.uuid4()),
        dimension="dashboard",
        criticality=criticality,
        summary=f"Test finding {criticality}",
        auto_fixable=auto_fixable,
        fix_hint="apply patch X" if auto_fixable else None,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocker_never_auto_fixed(tmp_path: Path) -> None:
    findings = [_finding(EnumDemoCriticality.DEMO_BLOCKER, auto_fixable=False)]
    report_path = _write_drift_report(tmp_path, findings)

    handler = HandlerDemoFixDispatcher()
    result = await handler.handle(
        ModelDemoFixDispatchRequest(
            run_id="test-run",
            drift_report_path=str(report_path),
            omni_home=str(tmp_path),
            dry_run=True,
        )
    )

    assert result.fixes_dispatched == 0
    assert result.fixes_skipped_human_approval >= 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_degraded_never_auto_fixed(tmp_path: Path) -> None:
    findings = [_finding(EnumDemoCriticality.DEMO_DEGRADED, auto_fixable=False)]
    report_path = _write_drift_report(tmp_path, findings)

    handler = HandlerDemoFixDispatcher()
    result = await handler.handle(
        ModelDemoFixDispatchRequest(
            run_id="test-run",
            drift_report_path=str(report_path),
            omni_home=str(tmp_path),
            dry_run=True,
        )
    )

    assert result.fixes_dispatched == 0
    assert result.fixes_skipped_human_approval >= 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cosmetic_dispatched_in_live_mode(tmp_path: Path) -> None:
    findings = [_finding(EnumDemoCriticality.COSMETIC, auto_fixable=True)]
    report_path = _write_drift_report(tmp_path, findings)

    handler = HandlerDemoFixDispatcher()
    result = await handler.handle(
        ModelDemoFixDispatchRequest(
            run_id="test-run",
            drift_report_path=str(report_path),
            omni_home=str(tmp_path),
            dry_run=False,
        )
    )

    assert result.fixes_dispatched == 1
    assert result.fixes_skipped_human_approval == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pr_limit_respected(tmp_path: Path) -> None:
    findings = [
        _finding(EnumDemoCriticality.COSMETIC, auto_fixable=True) for _ in range(8)
    ]
    report_path = _write_drift_report(tmp_path, findings)

    handler = HandlerDemoFixDispatcher()
    result = await handler.handle(
        ModelDemoFixDispatchRequest(
            run_id="test-run",
            drift_report_path=str(report_path),
            omni_home=str(tmp_path),
            max_open_autofix_prs=3,
            dry_run=False,
        )
    )

    assert result.fixes_dispatched == 3
    assert result.fixes_skipped_limit == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_log_written(tmp_path: Path) -> None:
    findings = [_finding(EnumDemoCriticality.COSMETIC, auto_fixable=True)]
    report_path = _write_drift_report(tmp_path, findings)

    handler = HandlerDemoFixDispatcher()
    result = await handler.handle(
        ModelDemoFixDispatchRequest(
            run_id="test-run",
            drift_report_path=str(report_path),
            omni_home=str(tmp_path),
            dry_run=False,
        )
    )

    log_path = Path(result.dispatch_log_path)
    assert log_path.exists()
    data = json.loads(log_path.read_text())
    assert data["run_id"] == "test-run"
    assert isinstance(data["records"], list)
