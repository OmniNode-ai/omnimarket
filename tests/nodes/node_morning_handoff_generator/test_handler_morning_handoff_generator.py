# SPDX-License-Identifier: MIT
"""Unit tests for HandlerMorningHandoffGenerator.

Verifies plan generation from complete evidence, partial evidence (missing files),
blocker counting, wave construction, dry_run, and artifact writing.
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
    ModelRehearsalBundle,
)
from omnimarket.nodes.node_demo_drift_detector.handlers.handler_demo_drift_detector import (
    ModelDemoDriftReport,
)
from omnimarket.nodes.node_demo_fix_dispatcher.handlers.handler_demo_fix_dispatcher import (
    ModelBoundedConcurrencyConfig,
    ModelFixDispatchLog,
    ModelFixDispatchRecord,
)
from omnimarket.nodes.node_morning_handoff_generator.handlers.handler_morning_handoff_generator import (
    HandlerMorningHandoffGenerator,
    ModelMorningHandoffRequest,
)


def _evidence_dir(tmp_path: Path, run_id: str) -> Path:
    """Return the path the handler will resolve for the given run_id."""
    return tmp_path / "docs" / "evidence" / "demo-readiness" / run_id


def _write_evidence(
    evidence_dir: Path,
    rehearsal_status: str = "GREEN",
    findings: list[ModelDriftFinding] | None = None,
    dispatched_ids: set[str] | None = None,
) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)

    bundle = ModelRehearsalBundle(
        rehearsal_id="r-001",
        timestamp_utc=datetime.now(UTC),
        runtime_topology_manifest={"nodes": 1},
        overall_status=rehearsal_status,
        failures=[],
    )
    (evidence_dir / "rehearsal_bundle.json").write_text(
        bundle.model_dump_json(), encoding="utf-8"
    )

    if findings is not None:
        report = ModelDemoDriftReport(
            run_id="test-run",
            detected_at=datetime.now(UTC),
            proof_of_green_rehearsal_id="r-001",
            findings=findings,
            demo_blocker_count=sum(
                1 for f in findings if f.criticality == EnumDemoCriticality.DEMO_BLOCKER
            ),
            demo_degraded_count=sum(
                1
                for f in findings
                if f.criticality == EnumDemoCriticality.DEMO_DEGRADED
            ),
        )
        (evidence_dir / "drift_report.json").write_text(
            report.model_dump_json(), encoding="utf-8"
        )

    if dispatched_ids is not None and findings is not None:
        records = [
            ModelFixDispatchRecord(
                finding_id=f.finding_id,
                criticality=f.criticality,
                summary=f.summary,
                dispatched=f.finding_id in dispatched_ids,
            )
            for f in findings
        ]
        log = ModelFixDispatchLog(
            run_id="test-run",
            dispatched_at=datetime.now(UTC),
            concurrency_config=ModelBoundedConcurrencyConfig(),
            records=records,
            fixes_dispatched=len(dispatched_ids),
        )
        (evidence_dir / "fix_dispatch_log.json").write_text(
            log.model_dump_json(), encoding="utf-8"
        )


def _blocker_finding() -> ModelDriftFinding:
    return ModelDriftFinding(
        finding_id=str(uuid.uuid4()),
        dimension="topology",
        criticality=EnumDemoCriticality.DEMO_BLOCKER,
        summary="Topology unreachable",
        auto_fixable=False,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_green_run_no_blockers(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path, "test-run")
    _write_evidence(evidence_dir, rehearsal_status="GREEN", findings=[])

    handler = HandlerMorningHandoffGenerator()
    result = await handler.handle(
        ModelMorningHandoffRequest(
            run_id="test-run",
            omni_home=str(tmp_path),
            dry_run=True,
        )
    )

    assert result.demo_blocker_count == 0
    assert "GREEN" in result.human_summary
    assert result.morning_dispatch_plan.proposed_dispatch_waves == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocker_appears_in_plan(tmp_path: Path) -> None:
    finding = _blocker_finding()
    evidence_dir = _evidence_dir(tmp_path, "test-run")
    _write_evidence(evidence_dir, findings=[finding], dispatched_ids=set())

    handler = HandlerMorningHandoffGenerator()
    result = await handler.handle(
        ModelMorningHandoffRequest(
            run_id="test-run",
            omni_home=str(tmp_path),
            dry_run=True,
        )
    )

    assert result.demo_blocker_count == 1
    assert len(result.morning_dispatch_plan.proposed_dispatch_waves) >= 1
    wave = result.morning_dispatch_plan.proposed_dispatch_waves[0]
    assert wave["requires_human_approval"] is True
    assert finding.finding_id in wave["issue_ids"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plan_written_to_disk(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path, "test-run")
    _write_evidence(evidence_dir, findings=[])

    handler = HandlerMorningHandoffGenerator()
    result = await handler.handle(
        ModelMorningHandoffRequest(
            run_id="test-run",
            omni_home=str(tmp_path),
            dry_run=False,
        )
    )

    plan_path = Path(result.plan_path)
    assert plan_path.exists()
    data = json.loads(plan_path.read_text())
    assert "overnight_summary" in data
    assert "issues" in data


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_evidence_files_handled_gracefully(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path, "empty-run")
    evidence_dir.mkdir(parents=True, exist_ok=True)

    handler = HandlerMorningHandoffGenerator()
    result = await handler.handle(
        ModelMorningHandoffRequest(
            run_id="empty-run",
            omni_home=str(tmp_path),
            dry_run=True,
        )
    )

    assert "No rehearsal bundle" in result.human_summary
    assert result.demo_blocker_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_no_artifact(tmp_path: Path) -> None:
    evidence_dir = _evidence_dir(tmp_path, "test-run")
    _write_evidence(evidence_dir, findings=[])

    handler = HandlerMorningHandoffGenerator()
    result = await handler.handle(
        ModelMorningHandoffRequest(
            run_id="test-run",
            omni_home=str(tmp_path),
            dry_run=True,
        )
    )

    assert result.dry_run is True
    assert not Path(result.plan_path).exists()
