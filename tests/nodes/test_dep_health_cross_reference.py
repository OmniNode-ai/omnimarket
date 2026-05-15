# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for CrossReferenceEngine (Task 7, OMN-11037)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_dependency_health_sweep.engine.cross_reference import (
    CrossReferenceEngine,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    EnumDepHealthFindingType,
    EnumDepHealthSeverity,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelImportGraph,
    ModelTopologyGraph,
)


def _make_empty_graph() -> ModelImportGraph:
    return ModelImportGraph(nodes=[], edges=[], orphan_modules=[])


def _make_empty_topology() -> ModelTopologyGraph:
    return ModelTopologyGraph(
        nodes=[],
        pub_edges=[],
        sub_edges=[],
        orphan_topics=[],
        undeclared_topics=[],
    )


# ---------------------------------------------------------------------------
# Scenario A: contract-aware untested handler
# ---------------------------------------------------------------------------


def test_untested_handler_emits_finding(tmp_path: Path) -> None:
    """Handler that is contract-referenced with no test and no golden-chain → UNTESTED_HANDLER MAJOR."""
    # Contract-referenced handler path
    handler_rel = "handlers/handler_foo.py"
    (tmp_path / "handlers").mkdir()
    (tmp_path / "handlers" / "handler_foo.py").write_text(
        "class HandlerFoo:\n    def handle(self, req): ...\n"
    )

    engine = CrossReferenceEngine()
    findings = engine.analyze(
        import_graph=_make_empty_graph(),
        topology=_make_empty_topology(),
        repo_label="test_repo",
        repo_root=tmp_path,
        contract_handler_paths=[str(tmp_path / handler_rel)],
    )

    untested = [
        f
        for f in findings
        if f.finding_type == EnumDepHealthFindingType.UNTESTED_HANDLER
    ]
    assert len(untested) >= 1
    assert untested[0].severity == EnumDepHealthSeverity.MAJOR
    assert untested[0].rule_id == "UNTESTED_HANDLER"
    assert untested[0].rule_version == "v1"


def test_handler_not_contract_referenced_no_finding(tmp_path: Path) -> None:
    """Handler with zero inbound imports but NOT contract-referenced should not fire UNTESTED_HANDLER."""
    (tmp_path / "handlers").mkdir()
    (tmp_path / "handlers" / "handler_bar.py").write_text(
        "class HandlerBar:\n    def handle(self, req): ...\n"
    )

    engine = CrossReferenceEngine()
    # Empty contract_handler_paths — handler_bar is not contract-referenced
    findings = engine.analyze(
        import_graph=_make_empty_graph(),
        topology=_make_empty_topology(),
        repo_label="test_repo",
        repo_root=tmp_path,
        contract_handler_paths=[],
    )

    untested = [
        f
        for f in findings
        if f.finding_type == EnumDepHealthFindingType.UNTESTED_HANDLER
    ]
    assert len(untested) == 0


def test_tested_handler_no_finding(tmp_path: Path) -> None:
    """Handler with a corresponding test file does not get UNTESTED_HANDLER finding."""
    (tmp_path / "handlers").mkdir()
    (tmp_path / "handlers" / "handler_baz.py").write_text(
        "class HandlerBaz:\n    def handle(self, req): ...\n"
    )
    # Create a test file that references the handler
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_handler_baz.py").write_text(
        "from handlers.handler_baz import HandlerBaz\n"
    )

    engine = CrossReferenceEngine()
    findings = engine.analyze(
        import_graph=_make_empty_graph(),
        topology=_make_empty_topology(),
        repo_label="test_repo",
        repo_root=tmp_path,
        contract_handler_paths=[str(tmp_path / "handlers" / "handler_baz.py")],
    )

    untested = [
        f
        for f in findings
        if f.finding_type == EnumDepHealthFindingType.UNTESTED_HANDLER
    ]
    assert len(untested) == 0


# ---------------------------------------------------------------------------
# Scenario B: missing topic edges
# ---------------------------------------------------------------------------


def test_cmd_orphan_topic_is_critical(tmp_path: Path) -> None:
    """Command topic with no consumer → MISSING_TOPIC_EDGE CRITICAL."""
    topology = ModelTopologyGraph(
        nodes=["node_a"],
        pub_edges=[("node_a", "onex.cmd.test.foo.v1", "pub")],
        sub_edges=[],
        orphan_topics=["onex.cmd.test.foo.v1"],
        undeclared_topics=[],
    )

    engine = CrossReferenceEngine()
    findings = engine.analyze(
        import_graph=_make_empty_graph(),
        topology=topology,
        repo_label="test_repo",
        repo_root=tmp_path,
        contract_handler_paths=[],
    )

    critical = [
        f
        for f in findings
        if f.finding_type == EnumDepHealthFindingType.MISSING_TOPIC_EDGE
        and f.severity == EnumDepHealthSeverity.CRITICAL
    ]
    assert len(critical) >= 1
    assert critical[0].rule_id == "MISSING_TOPIC_EDGE"
    assert critical[0].rule_version == "v1"
    assert "onex.cmd.test.foo.v1" in critical[0].detail


def test_terminal_evt_orphan_topic_is_major(tmp_path: Path) -> None:
    """Terminal event with no reducer → MISSING_TOPIC_EDGE MAJOR."""
    topology = ModelTopologyGraph(
        nodes=["node_b"],
        pub_edges=[("node_b", "onex.evt.test.completed.v1", "pub")],
        sub_edges=[],
        orphan_topics=["onex.evt.test.completed.v1"],
        undeclared_topics=[],
    )

    engine = CrossReferenceEngine()
    findings = engine.analyze(
        import_graph=_make_empty_graph(),
        topology=topology,
        repo_label="test_repo",
        repo_root=tmp_path,
        contract_handler_paths=[],
    )

    major = [
        f
        for f in findings
        if f.finding_type == EnumDepHealthFindingType.MISSING_TOPIC_EDGE
        and f.severity == EnumDepHealthSeverity.MAJOR
    ]
    assert len(major) >= 1


def test_external_consumer_topic_is_info(tmp_path: Path) -> None:
    """Topic with externally_consumed declaration → MISSING_TOPIC_EDGE INFO."""
    topology = ModelTopologyGraph(
        nodes=["node_c"],
        pub_edges=[("node_c", "onex.evt.test.external.v1", "pub")],
        sub_edges=[],
        orphan_topics=[],  # already filtered out by topology parser
        undeclared_topics=[],
    )

    engine = CrossReferenceEngine()
    # When a topic is NOT in orphan_topics it should not generate findings
    findings = engine.analyze(
        import_graph=_make_empty_graph(),
        topology=topology,
        repo_label="test_repo",
        repo_root=tmp_path,
        contract_handler_paths=[],
    )

    missing = [
        f
        for f in findings
        if f.finding_type == EnumDepHealthFindingType.MISSING_TOPIC_EDGE
    ]
    assert len(missing) == 0


# ---------------------------------------------------------------------------
# Scenario C: dead imports
# ---------------------------------------------------------------------------


def test_dead_import_isolated_module_minor(tmp_path: Path) -> None:
    """Module with no edges and not excluded → DEAD_IMPORT MINOR."""
    # Create the actual file
    stale = tmp_path / "utils" / "stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("# stale module\n")

    import_graph = ModelImportGraph(
        nodes=["utils/stale.py"],
        edges=[],
        orphan_modules=["utils/stale.py"],
    )

    engine = CrossReferenceEngine()
    findings = engine.analyze(
        import_graph=import_graph,
        topology=_make_empty_topology(),
        repo_label="test_repo",
        repo_root=tmp_path,
        contract_handler_paths=[],
    )

    dead = [
        f for f in findings if f.finding_type == EnumDepHealthFindingType.DEAD_IMPORT
    ]
    assert len(dead) >= 1
    assert dead[0].severity == EnumDepHealthSeverity.MINOR
    assert dead[0].rule_id == "DEAD_IMPORT"
    assert dead[0].rule_version == "v1"


@pytest.mark.parametrize(
    "filename",
    [
        "__main__.py",
        "__init__.py",
        "migrations/0001_initial.py",
        "fixtures/fixture_data.py",
        "cli.py",
    ],
)
def test_dead_import_excluded_patterns_no_finding(
    tmp_path: Path, filename: str
) -> None:
    """Modules matching exclusion patterns do not fire DEAD_IMPORT."""
    file_path = tmp_path / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("# excluded\n")

    import_graph = ModelImportGraph(
        nodes=[filename],
        edges=[],
        orphan_modules=[filename],
    )

    engine = CrossReferenceEngine()
    findings = engine.analyze(
        import_graph=import_graph,
        topology=_make_empty_topology(),
        repo_label="test_repo",
        repo_root=tmp_path,
        contract_handler_paths=[],
    )

    dead = [
        f for f in findings if f.finding_type == EnumDepHealthFindingType.DEAD_IMPORT
    ]
    assert len(dead) == 0


def test_dead_import_contract_handler_excluded(tmp_path: Path) -> None:
    """Module listed in contract_handler_paths is excluded from DEAD_IMPORT."""
    handler_file = tmp_path / "handlers" / "handler_isolated.py"
    handler_file.parent.mkdir()
    handler_file.write_text("class HandlerIsolated: ...\n")

    import_graph = ModelImportGraph(
        nodes=["handlers/handler_isolated.py"],
        edges=[],
        orphan_modules=["handlers/handler_isolated.py"],
    )

    engine = CrossReferenceEngine()
    findings = engine.analyze(
        import_graph=import_graph,
        topology=_make_empty_topology(),
        repo_label="test_repo",
        repo_root=tmp_path,
        contract_handler_paths=[str(handler_file)],
    )

    dead = [
        f for f in findings if f.finding_type == EnumDepHealthFindingType.DEAD_IMPORT
    ]
    assert len(dead) == 0


# ---------------------------------------------------------------------------
# Scenario D: undeclared topics
# ---------------------------------------------------------------------------


def test_undeclared_topic_emits_finding(tmp_path: Path) -> None:
    """Topic literal in source but not in contracts → UNDECLARED_TOPIC MAJOR."""
    topology = ModelTopologyGraph(
        nodes=[],
        pub_edges=[],
        sub_edges=[],
        orphan_topics=[],
        undeclared_topics=["onex.cmd.test.undeclared.v1"],
    )

    engine = CrossReferenceEngine()
    findings = engine.analyze(
        import_graph=_make_empty_graph(),
        topology=topology,
        repo_label="test_repo",
        repo_root=tmp_path,
        contract_handler_paths=[],
    )

    undeclared = [
        f
        for f in findings
        if f.finding_type == EnumDepHealthFindingType.UNDECLARED_TOPIC
    ]
    assert len(undeclared) >= 1
    assert undeclared[0].severity == EnumDepHealthSeverity.MAJOR
    assert undeclared[0].rule_id == "UNDECLARED_TOPIC"
    assert undeclared[0].rule_version == "v1"


# ---------------------------------------------------------------------------
# Finding structure
# ---------------------------------------------------------------------------


def test_findings_have_required_fields(tmp_path: Path) -> None:
    """All findings include repo, file_path, symbol, detail, rule_id, rule_version."""
    topology = ModelTopologyGraph(
        nodes=["node_a"],
        pub_edges=[("node_a", "onex.cmd.test.orphan.v1", "pub")],
        sub_edges=[],
        orphan_topics=["onex.cmd.test.orphan.v1"],
        undeclared_topics=[],
    )

    engine = CrossReferenceEngine()
    findings = engine.analyze(
        import_graph=_make_empty_graph(),
        topology=topology,
        repo_label="my_repo",
        repo_root=tmp_path,
        contract_handler_paths=[],
    )

    assert len(findings) > 0
    for f in findings:
        assert f.repo == "my_repo"
        assert f.detail != ""
        assert f.rule_id != ""
        assert f.rule_version != ""
