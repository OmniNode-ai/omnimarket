# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for BaselineDiffEngine (Task 8, OMN-11038)."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_dependency_health_sweep.engine.baseline_diff import (
    BaselineDiffEngine,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    EnumDepHealthFindingType,
    EnumDepHealthSeverity,
    ModelDepHealthFinding,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelBaselineSnapshot,
)


def _make_finding(
    detail: str = "test detail",
    file_path: str = "src/module.py",
    symbol: str | None = "foo",
    finding_type: EnumDepHealthFindingType = EnumDepHealthFindingType.DEAD_IMPORT,
    severity: EnumDepHealthSeverity = EnumDepHealthSeverity.MINOR,
) -> ModelDepHealthFinding:
    return ModelDepHealthFinding(
        finding_type=finding_type,
        severity=severity,
        repo="test_repo",
        file_path=file_path,
        symbol=symbol,
        detail=detail,
        rule_id=finding_type.value,
        rule_version="v1",
    )


# ---------------------------------------------------------------------------
# Basic diff: new vs. resolved
# ---------------------------------------------------------------------------


def test_diff_new_finding_detected(tmp_path: Path) -> None:
    """Finding C in current but absent from baseline → new_findings=[C], delta=1."""
    finding_a = _make_finding(detail="finding A", file_path="src/a.py")
    finding_b = _make_finding(detail="finding B", file_path="src/b.py")
    finding_c = _make_finding(detail="finding C", file_path="src/c.py")

    baseline = ModelBaselineSnapshot(
        findings=[finding_a, finding_b],
        graphify_version="v0.8.2",
        rule_version="v1",
        captured_at="2025-01-01T00:00:00",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json())

    engine = BaselineDiffEngine()
    result = engine.diff(
        current=[finding_a, finding_b, finding_c],
        baseline_path=baseline_path,
    )

    assert result.delta == 1
    assert len(result.new_findings) == 1
    assert result.new_findings[0].file_path == "src/c.py"
    assert len(result.resolved_findings) == 0


def test_diff_resolved_finding_detected(tmp_path: Path) -> None:
    """Finding in baseline absent from current → resolved_findings, delta unchanged."""
    finding_a = _make_finding(detail="finding A", file_path="src/a.py")
    finding_b = _make_finding(detail="finding B", file_path="src/b.py")

    baseline = ModelBaselineSnapshot(
        findings=[finding_a, finding_b],
        graphify_version="v0.8.2",
        rule_version="v1",
        captured_at="2025-01-01T00:00:00",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json())

    engine = BaselineDiffEngine()
    result = engine.diff(current=[finding_a], baseline_path=baseline_path)

    assert result.delta == -1
    assert len(result.resolved_findings) == 1
    assert result.resolved_findings[0].file_path == "src/b.py"
    assert len(result.new_findings) == 0


def test_diff_no_changes(tmp_path: Path) -> None:
    """Identical findings → delta=0, no new, no resolved."""
    finding_a = _make_finding(detail="finding A", file_path="src/a.py")

    baseline = ModelBaselineSnapshot(
        findings=[finding_a],
        graphify_version="v0.8.2",
        rule_version="v1",
        captured_at="2025-01-01T00:00:00",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json())

    engine = BaselineDiffEngine()
    result = engine.diff(current=[finding_a], baseline_path=baseline_path)

    assert result.delta == 0
    assert len(result.new_findings) == 0
    assert len(result.resolved_findings) == 0


def test_diff_no_baseline_path_returns_none_delta() -> None:
    """When baseline_path is None, diff is skipped and None is returned."""
    finding_a = _make_finding(detail="finding A", file_path="src/a.py")

    engine = BaselineDiffEngine()
    result = engine.diff(current=[finding_a], baseline_path=None)

    # Returns a result with delta=None indicating no baseline comparison
    assert result is None


# ---------------------------------------------------------------------------
# Composite key correctness — different detail/version = different finding
# ---------------------------------------------------------------------------


def test_diff_detail_hash_distinguishes_findings(tmp_path: Path) -> None:
    """Two findings that differ only in detail text are treated as distinct."""
    finding_v1 = _make_finding(detail="original detail", file_path="src/a.py")
    finding_v2 = _make_finding(detail="updated detail text", file_path="src/a.py")

    baseline = ModelBaselineSnapshot(
        findings=[finding_v1],
        graphify_version="v0.8.2",
        rule_version="v1",
        captured_at="2025-01-01T00:00:00",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json())

    engine = BaselineDiffEngine()
    result = engine.diff(current=[finding_v2], baseline_path=baseline_path)

    # v2 has different detail → treated as new; v1 is resolved
    assert result.delta == 0  # net: +1 new, -1 resolved
    assert len(result.new_findings) == 1
    assert len(result.resolved_findings) == 1


def test_diff_graphify_version_in_composite_key(tmp_path: Path) -> None:
    """Findings from a different graphify_version are treated as new."""
    finding_old = _make_finding(detail="same detail", file_path="src/a.py")

    # Store baseline with old graphify version embedded in snapshot metadata.
    # The engine uses the snapshot's graphify_version for composite key
    # comparison when it reads the baseline.
    baseline = ModelBaselineSnapshot(
        findings=[finding_old],
        graphify_version="v0.8.1",
        rule_version="v1",
        captured_at="2025-01-01T00:00:00",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json())

    engine = BaselineDiffEngine()
    # Current findings produced with a different graphify version
    result = engine.diff(
        current=[finding_old],
        baseline_path=baseline_path,
        current_graphify_version="v0.8.2",
    )

    # Different graphify_version in composite key → finding treated as new
    assert len(result.new_findings) == 1
    assert len(result.resolved_findings) == 1


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_diff_missing_baseline_file_returns_none(tmp_path: Path) -> None:
    """Non-existent baseline file is treated as 'no baseline' — returns None."""
    finding_a = _make_finding(detail="finding A", file_path="src/a.py")
    missing_path = tmp_path / "nonexistent_baseline.json"

    engine = BaselineDiffEngine()
    result = engine.diff(current=[finding_a], baseline_path=missing_path)

    assert result is None


def test_diff_empty_baseline_and_no_findings(tmp_path: Path) -> None:
    """Empty baseline + empty current → delta=0."""
    baseline = ModelBaselineSnapshot(
        findings=[],
        graphify_version="v0.8.2",
        rule_version="v1",
        captured_at="2025-01-01T00:00:00",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json())

    engine = BaselineDiffEngine()
    result = engine.diff(current=[], baseline_path=baseline_path)

    assert result.delta == 0
    assert result.new_findings == []
    assert result.resolved_findings == []
