# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_dependency_health_sweep models (TDD — written before implementation)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_model_dep_health_sweep_request_fields() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_request import (
        ModelDepHealthSweepRequest,
    )

    req = ModelDepHealthSweepRequest(
        repo_roots=["src/"],
        severity_threshold="MAJOR",
        dry_run=True,
    )
    assert req.repo_roots == ["src/"]
    assert req.scope is None
    assert req.severity_threshold == "MAJOR"
    assert req.dry_run is True
    assert req.baseline_path is None
    assert req.run_id is None


@pytest.mark.unit
def test_model_dep_health_sweep_request_with_run_id() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_request import (
        ModelDepHealthSweepRequest,
    )

    req = ModelDepHealthSweepRequest(
        repo_roots=["src/"],
        severity_threshold="CRITICAL",
        dry_run=False,
        run_id="test-run-001",
    )
    assert req.run_id == "test-run-001"


@pytest.mark.unit
def test_model_dep_health_finding_fields() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
        EnumDepHealthFindingType,
        EnumDepHealthSeverity,
        ModelDepHealthFinding,
    )

    finding = ModelDepHealthFinding(
        finding_type=EnumDepHealthFindingType.ORPHAN_IMPORT,
        severity=EnumDepHealthSeverity.MAJOR,
        repo="omnimarket",
        detail="Module foo has no inbound imports",
        rule_id="ORPHAN_IMPORT",
        rule_version="v1",
    )
    assert finding.finding_type == EnumDepHealthFindingType.ORPHAN_IMPORT
    assert finding.severity == EnumDepHealthSeverity.MAJOR
    assert finding.repo == "omnimarket"
    assert finding.file_path is None
    assert finding.symbol is None
    assert finding.detail == "Module foo has no inbound imports"
    assert finding.rule_id == "ORPHAN_IMPORT"
    assert finding.rule_version == "v1"


@pytest.mark.unit
def test_enum_dep_health_finding_type_values() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
        EnumDepHealthFindingType,
    )

    assert EnumDepHealthFindingType.ORPHAN_IMPORT == "ORPHAN_IMPORT"
    assert EnumDepHealthFindingType.MISSING_TOPIC_EDGE == "MISSING_TOPIC_EDGE"
    assert EnumDepHealthFindingType.DEAD_IMPORT == "DEAD_IMPORT"
    assert EnumDepHealthFindingType.UNTESTED_HANDLER == "UNTESTED_HANDLER"
    assert EnumDepHealthFindingType.CONTRACT_DRIFT == "CONTRACT_DRIFT"
    assert EnumDepHealthFindingType.UNDECLARED_TOPIC == "UNDECLARED_TOPIC"


@pytest.mark.unit
def test_enum_dep_health_severity_values() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
        EnumDepHealthSeverity,
    )

    assert EnumDepHealthSeverity.CRITICAL == "CRITICAL"
    assert EnumDepHealthSeverity.MAJOR == "MAJOR"
    assert EnumDepHealthSeverity.MINOR == "MINOR"
    assert EnumDepHealthSeverity.INFO == "INFO"


@pytest.mark.unit
def test_model_dep_health_sweep_result_fields() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_result import (
        ModelDepHealthSweepResult,
    )

    result = ModelDepHealthSweepResult(
        status="clean",
        run_id="test-run-001",
        findings=[],
        summary={},
        graphify_version="unknown",
    )
    assert result.status == "clean"
    assert result.run_id == "test-run-001"
    assert result.findings == []
    assert result.summary == {}
    assert result.baseline_delta is None
    assert result.graphify_version == "unknown"


@pytest.mark.unit
def test_model_graph_types_exist() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
        ModelBaselineSnapshot,
        ModelDiffResult,
        ModelImportGraph,
        ModelTopologyGraph,
    )

    import_graph = ModelImportGraph(nodes=[], edges=[], orphan_modules=[])
    assert import_graph.nodes == []

    topology = ModelTopologyGraph(
        nodes=[],
        pub_edges=[],
        sub_edges=[],
        orphan_topics=[],
        undeclared_topics=[],
    )
    assert topology.nodes == []

    baseline = ModelBaselineSnapshot(
        findings=[],
        graphify_version="unknown",
        rule_version="v1",
        captured_at="2025-01-01T00:00:00Z",
    )
    assert baseline.rule_version == "v1"

    diff = ModelDiffResult(new_findings=[], resolved_findings=[], delta=0)
    assert diff.delta == 0


@pytest.mark.unit
def test_model_dep_health_sweep_completed_event_captured_at_is_datetime() -> None:
    from datetime import UTC, datetime

    from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_completed_event import (
        ModelDepHealthSweepCompletedEvent,
    )

    now = datetime.now(UTC)
    event = ModelDepHealthSweepCompletedEvent(
        run_id="test-run-001",
        findings=[],
        summary={},
        captured_at=now,
    )
    assert isinstance(event.captured_at, datetime)
    assert event.captured_at == now


@pytest.mark.unit
def test_models_are_frozen() -> None:
    from pydantic import ValidationError

    from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_request import (
        ModelDepHealthSweepRequest,
    )

    req = ModelDepHealthSweepRequest(
        repo_roots=["src/"],
        severity_threshold="MAJOR",
        dry_run=False,
    )
    with pytest.raises((ValidationError, TypeError)):
        req.repo_roots = ["other/"]  # type: ignore[misc]


@pytest.mark.unit
def test_models_forbid_extra_fields() -> None:
    from pydantic import ValidationError

    from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_request import (
        ModelDepHealthSweepRequest,
    )

    with pytest.raises(ValidationError):
        ModelDepHealthSweepRequest(
            repo_roots=["src/"],
            severity_threshold="MAJOR",
            dry_run=False,
            unknown_field="should_fail",  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_all_models_importable_from_package() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.models import (
        EnumDepHealthFindingType,
        EnumDepHealthSeverity,
        ModelBaselineSnapshot,
        ModelDepHealthFinding,
        ModelDepHealthSweepCompletedEvent,
        ModelDepHealthSweepRequest,
        ModelDepHealthSweepResult,
        ModelDiffResult,
        ModelImportGraph,
        ModelTopologyGraph,
    )

    assert EnumDepHealthFindingType is not None
    assert EnumDepHealthSeverity is not None
    assert ModelDepHealthSweepRequest is not None
    assert ModelDepHealthFinding is not None
    assert ModelDepHealthSweepResult is not None
    assert ModelImportGraph is not None
    assert ModelTopologyGraph is not None
    assert ModelBaselineSnapshot is not None
    assert ModelDiffResult is not None
    assert ModelDepHealthSweepCompletedEvent is not None
