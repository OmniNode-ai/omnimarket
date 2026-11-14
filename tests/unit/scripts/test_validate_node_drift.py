# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/validate_node_drift.py.

Regression coverage:
- Pyproject-only diffs must not strict-fail on KNOWN_MAIN_VIOLATIONS nodes.
- Directly modified nodes still receive --strict promotion.
- collect_nodes returns the strict-eligible set so run() can scope strict per-node.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "validate_node_drift.py"


def _load_drift_module() -> object:
    spec = importlib.util.spec_from_file_location("validate_node_drift", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_node_drift"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def drift_module() -> object:
    return _load_drift_module()


@pytest.mark.unit
@pytest.mark.parametrize("changed_ref", [None, "HEAD"])
@pytest.mark.parametrize("condition", ["live", "repaired", "deleted"])
def test_stale_known_violation_gate(
    drift_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    changed_ref: str | None,
    condition: str,
) -> None:
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    node = nodes / "node_example"
    if condition != "deleted":
        node.mkdir()
        (node / "contract.yaml").write_text(
            "name: node_example\nnode_type: effect\n"
            + ("handler: {}\n" if condition == "repaired" else "")
            # This advisory finding must not keep a repaired exemption alive.
            + "event_bus:\n  subscribe_topics: [onex.cmd.omnimarket.example.v1]\n"
        )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project.entry-points."onex.nodes"]\nnode_example = "example"\n'
    )
    monkeypatch.setattr(drift_module, "NODES_DIR", nodes)
    monkeypatch.setattr(drift_module, "PYPROJECT", pyproject)
    monkeypatch.setattr(drift_module, "KNOWN_MAIN_VIOLATIONS", {"node_example"})
    # The reviewed exemption list names real nodes, none of which is in this
    # temporary tree, so this test reads an empty one.
    no_exemptions = tmp_path / "exemptions.yaml"
    no_exemptions.write_text("exemptions: []\n")
    monkeypatch.setattr(drift_module, "ENTRY_POINT_EXEMPTIONS_FILE", no_exemptions)
    if changed_ref:
        monkeypatch.setattr(
            drift_module, "collect_nodes", lambda **_kwargs: ([], set())
        )
    code = drift_module.run(
        changed_ref=changed_ref,
        strict=False,
        check_orphan_nodes=False,
        output_json=True,
    )
    output = json.loads(capsys.readouterr().out)
    assert code == (0 if condition == "live" else 1)
    stale = [
        f
        for r in output["results"]
        for f in r["findings"]
        if f["check"] == "stale_baseline_entry"
    ]
    assert bool(stale) is (condition != "live")
    if stale:
        assert stale[0]["level"] == "FAIL"
        assert "remove it" in stale[0]["message"]


@pytest.mark.unit
def test_real_known_violations_have_no_stale_entries(drift_module: ModuleType) -> None:
    entries = drift_module._load_entry_points(REPO_ROOT / "pyproject.toml")
    assert drift_module._stale_known_main_violations(entries) == []


@pytest.mark.unit
def test_validate_node_strict_only_on_directly_modified(
    drift_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing-on-main violation MUST stay WARN unless the node itself was modified."""
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    monkeypatch.setattr(
        drift_module, "KNOWN_MAIN_VIOLATIONS", {"node_overseer_observer"}
    )
    # Build a node that intentionally lacks a handler block (effect type).
    bad_node = nodes_dir / "node_overseer_observer"  # in KNOWN_MAIN_VIOLATIONS
    bad_node.mkdir()
    (bad_node / "contract.yaml").write_text(
        "name: overseer_observer\nnode_type: compute\n"
    )

    # WARN path: node not directly modified -> should be WARN, not FAIL.
    warn_result = drift_module.validate_node(  # type: ignore[attr-defined]
        bad_node, entry_points=set(), strict=False
    )
    assert warn_result.passed is True, (
        "KNOWN_MAIN_VIOLATIONS node must remain WARN-only when strict=False"
    )
    assert warn_result.has_warn is True

    # FAIL path: same node, but treated as directly modified -> strict promotes WARN to FAIL.
    fail_result = drift_module.validate_node(  # type: ignore[attr-defined]
        bad_node, entry_points=set(), strict=True
    )
    assert fail_result.passed is False, (
        "KNOWN_MAIN_VIOLATIONS node must FAIL when directly modified (strict=True)"
    )


@pytest.mark.unit
def test_get_changed_nodes_pyproject_only_returns_no_strict_eligible(
    drift_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only pyproject.toml changes, no node is strict-eligible.

    This is the exact regression that broke OMN-9639 PR #412: any PR touching
    pyproject.toml was failing the gate on every pre-existing-on-main violation
    because all nodes were promoted to --strict.
    """
    # Stub out git diff to return only pyproject.toml.
    real_run = drift_module.subprocess.run  # type: ignore[attr-defined]

    class _StubProc:
        returncode = 0
        stdout = "pyproject.toml\n"
        stderr = ""

    def fake_run(*args: object, **kwargs: object) -> _StubProc:
        return _StubProc()

    monkeypatch.setattr(drift_module.subprocess, "run", fake_run)  # type: ignore[attr-defined]

    # Stub NODES_DIR with a couple of dirs so the all-nodes branch returns something.
    fake_nodes_dir = tmp_path / "src" / "omnimarket" / "nodes"
    fake_nodes_dir.mkdir(parents=True)
    (fake_nodes_dir / "node_alpha").mkdir()
    (fake_nodes_dir / "node_beta").mkdir()

    monkeypatch.setattr(drift_module, "NODES_DIR", fake_nodes_dir)

    try:
        nodes, directly_modified = drift_module._get_changed_nodes("origin/main")  # type: ignore[attr-defined]
    finally:
        # Restore subprocess.run for other tests in the session.
        monkeypatch.setattr(drift_module.subprocess, "run", real_run)  # type: ignore[attr-defined]

    assert {p.name for p in nodes} == {"node_alpha", "node_beta"}
    assert directly_modified == set(), (
        "pyproject-only diff must produce empty directly-modified set so strict mode is not applied"
    )


@pytest.mark.unit
def test_get_changed_nodes_node_source_change_marks_directly_modified(
    drift_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real node source edit should mark that node as directly modified (strict-eligible)."""

    class _StubProc:
        returncode = 0
        stdout = "src/omnimarket/nodes/node_alpha/handler.py\n"
        stderr = ""

    def _fake_run(*_args: object, **_kwargs: object) -> _StubProc:
        return _StubProc()

    monkeypatch.setattr(drift_module.subprocess, "run", _fake_run)  # type: ignore[attr-defined]

    fake_nodes_dir = tmp_path / "src" / "omnimarket" / "nodes"
    fake_nodes_dir.mkdir(parents=True)
    (fake_nodes_dir / "node_alpha").mkdir()
    monkeypatch.setattr(drift_module, "NODES_DIR", fake_nodes_dir)

    nodes, directly_modified = drift_module._get_changed_nodes("origin/main")  # type: ignore[attr-defined]
    assert {p.name for p in nodes} == {"node_alpha"}
    assert directly_modified == {"node_alpha"}


@pytest.mark.unit
def test_collect_nodes_check_all_returns_none_strict_eligible(
    drift_module: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check-all mode must yield strict_eligible=None so the caller's strict flag is uniform."""
    fake_nodes_dir = tmp_path / "src" / "omnimarket" / "nodes"
    fake_nodes_dir.mkdir(parents=True)
    (fake_nodes_dir / "node_alpha").mkdir()
    monkeypatch.setattr(drift_module, "NODES_DIR", fake_nodes_dir)

    nodes, strict_eligible = drift_module.collect_nodes(changed_ref=None)  # type: ignore[attr-defined]
    assert {p.name for p in nodes} == {"node_alpha"}
    assert strict_eligible is None


@pytest.mark.unit
def test_orphan_node_check_fails_known_violation_without_allowlist(
    drift_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Node liveness findings must not use KNOWN_MAIN_VIOLATIONS WARN mode."""
    monkeypatch.setattr(
        drift_module, "KNOWN_MAIN_VIOLATIONS", {"node_overseer_observer"}
    )
    bad_node = tmp_path / "node_overseer_observer"
    bad_node.mkdir()
    (bad_node / "contract.yaml").write_text(
        "name: node_overseer_observer\n"
        "node_type: compute\n"
        "event_bus:\n"
        "  subscribe_topics:\n"
        "    - onex.cmd.omnimarket.overseer-observer.v1\n"
    )

    result = drift_module.validate_node(  # type: ignore[attr-defined]
        bad_node,
        entry_points={"node_overseer_observer"},
        strict=False,
        check_orphan_nodes=True,
    )

    assert result.passed is False
    assert any(
        finding.check == "node_liveness"
        and finding.level == "FAIL"
        and "CONSUMER_ONLY" in finding.message
        for finding in result.findings
    )


@pytest.mark.unit
def test_orphan_node_check_allows_experimental_contract(
    drift_module: object,
    tmp_path: Path,
) -> None:
    node_dir = tmp_path / "node_experimental"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "name: node_experimental\n"
        "node_type: orchestrator\n"
        "lifecycle: experimental\n"
        "event_bus:\n"
        "  publish_topics:\n"
        "    - onex.evt.omnimarket.experimental-completed.v1\n"
    )

    result = drift_module.validate_node(  # type: ignore[attr-defined]
        node_dir,
        entry_points={"node_experimental"},
        check_orphan_nodes=True,
    )

    assert result.passed is True
    assert not any(finding.check == "node_liveness" for finding in result.findings)


def _write_node(tmp_path: Path, name: str, lifecycle: str) -> Path:
    node_dir = tmp_path / name
    node_dir.mkdir()
    lifecycle_line = f"lifecycle: {lifecycle}\n" if lifecycle else ""
    (node_dir / "contract.yaml").write_text(
        f"name: {name}\n"
        "node_type: effect\n"
        f"{lifecycle_line}"
        "handler:\n"
        f"  module: omnimarket.nodes.{name}.handlers.handler_{name}\n"
        "  class: HandlerNode\n"
    )
    return node_dir


@pytest.mark.unit
def test_pyproject_entry_check_allows_listed_exemption(
    drift_module: object,
    tmp_path: Path,
) -> None:
    """A node on the reviewed exemption list passes the pyproject_entry check
    even in --strict mode (OMN-14151 retired legacy nodes are listed, not
    excused by their lifecycle)."""
    node_dir = _write_node(tmp_path, "node_legacy_arm", "deprecated")

    result = drift_module.validate_node(  # type: ignore[attr-defined]
        node_dir,
        entry_points=set(),  # no pyproject entry — this node was deregistered
        strict=True,
        exemptions={"node_legacy_arm"},
    )

    assert result.passed is True
    assert not any(finding.check == "pyproject_entry" for finding in result.findings)


@pytest.mark.unit
@pytest.mark.parametrize("lifecycle", ["experimental", "deprecated"])
def test_pyproject_entry_check_fails_unlisted_exemption_by_lifecycle(
    drift_module: object,
    tmp_path: Path,
    lifecycle: str,
) -> None:
    """experimental or deprecated no longer excuses a missing entry point: the
    reviewed list is the only exemption, so the node inventory test and this
    gate judge the node alike."""
    node_dir = _write_node(tmp_path, "node_unlisted", lifecycle)

    result = drift_module.validate_node(  # type: ignore[attr-defined]
        node_dir,
        entry_points=set(),
        strict=True,
        exemptions=set(),
    )

    assert result.passed is False
    assert any(finding.check == "pyproject_entry" for finding in result.findings)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "is missing"),
        ("exemptions: not-a-list\n", "must be a list"),
        ("exemptions:\n  - node: node_a\n", "needs a non-empty 'reason'"),
        ("exemptions:\n  - node: node_a\n    reason: ''\n", "non-empty 'reason'"),
        (
            "exemptions:\n  - node: node_a\n    reason: r\n"
            "  - node: node_a\n    reason: r\n",
            "node_a is listed twice",
        ),
        ("exemptions:\n  - reason: r\n", "non-empty 'node'"),
    ],
    ids=[
        "missing-file",
        "not-a-list",
        "no-reason",
        "empty-reason",
        "duplicate-node",
        "no-node",
    ],
)
def test_load_entry_point_exemptions_refuses_bad_files(
    drift_module: object,
    tmp_path: Path,
    content: str | None,
    message: str,
) -> None:
    path = tmp_path / "exemptions.yaml"
    if content is not None:
        path.write_text(content)

    with pytest.raises(ValueError, match=message):
        drift_module.load_entry_point_exemptions(path)  # type: ignore[attr-defined]


@pytest.mark.unit
def test_load_entry_point_exemptions_reads_node_and_reason(
    drift_module: object,
    tmp_path: Path,
) -> None:
    path = tmp_path / "exemptions.yaml"
    path.write_text("exemptions:\n  - node: node_a\n    reason: runs in process\n")

    loaded = drift_module.load_entry_point_exemptions(path)  # type: ignore[attr-defined]

    assert loaded == {"node_a": "runs in process"}


@pytest.mark.unit
def test_real_exemption_list_has_no_stale_entries(
    drift_module: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    entries = drift_module._load_entry_points(REPO_ROOT / "pyproject.toml")  # type: ignore[attr-defined]

    assert drift_module._stale_entry_point_exemptions(entries) == []  # type: ignore[attr-defined]


@pytest.mark.unit
def test_stale_entry_point_exemption_is_reported(
    drift_module: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: a listed node that has gained an entry point is stale."""
    monkeypatch.chdir(REPO_ROOT)
    listed = sorted(drift_module.load_entry_point_exemptions())[0]  # type: ignore[attr-defined]

    stale = drift_module._stale_entry_point_exemptions({listed})  # type: ignore[attr-defined]

    assert stale == [listed]


@pytest.mark.unit
def test_every_real_exemption_passes_the_gate_entry_check(
    drift_module: object,
) -> None:
    exemptions = drift_module.load_entry_point_exemptions()  # type: ignore[attr-defined]
    assert exemptions
    for name in exemptions:
        result = drift_module.validate_node(  # type: ignore[attr-defined]
            REPO_ROOT / "src" / "omnimarket" / "nodes" / name,
            entry_points=set(),
            strict=True,
        )
        assert not any(f.check == "pyproject_entry" for f in result.findings), name


@pytest.mark.unit
def test_pyproject_entry_check_fails_missing_entry_without_lifecycle_marker(
    drift_module: object,
    tmp_path: Path,
) -> None:
    """A node missing its pyproject entry with NO lifecycle marker still FAILs —
    the reviewed exemption list is opt-in, not a blanket loosening of check 3."""
    node_dir = tmp_path / "node_unregistered"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "name: node_unregistered\n"
        "node_type: effect\n"
        "handler:\n"
        "  module: omnimarket.nodes.node_unregistered.handlers.handler_unregistered\n"
        "  class: HandlerUnregistered\n"
    )

    result = drift_module.validate_node(  # type: ignore[attr-defined]
        node_dir,
        entry_points=set(),
        strict=True,
    )

    assert result.passed is False
    assert any(finding.check == "pyproject_entry" for finding in result.findings)


@pytest.mark.unit
def test_orphan_node_check_passes_bidirectional_contract(
    drift_module: object,
    tmp_path: Path,
) -> None:
    node_dir = tmp_path / "node_wired"
    node_dir.mkdir()
    (node_dir / "contract.yaml").write_text(
        "name: node_wired\n"
        "node_type: compute\n"
        "handler:\n"
        "  module: omnimarket.nodes.node_wired.handlers.handler_wired\n"
        "  class: HandlerWired\n"
        "event_bus:\n"
        "  subscribe_topics:\n"
        "    - onex.cmd.omnimarket.wired-start.v1\n"
        "  publish_topics:\n"
        "    - onex.evt.omnimarket.wired-completed.v1\n"
    )

    result = drift_module.validate_node(  # type: ignore[attr-defined]
        node_dir,
        entry_points={"node_wired"},
        check_orphan_nodes=True,
    )

    assert result.passed is True


@pytest.mark.unit
def test_contractless_node_declared_not_full_runtime_passes(
    drift_module: ModuleType, tmp_path: Path
) -> None:
    """A migrations-only directory declares full_runtime false and has no contract."""
    stub = tmp_path / "node_migrations_only"
    stub.mkdir()
    (stub / "metadata.yaml").write_text(
        "name: migrations_only\ncapabilities:\n  full_runtime: false\n"
    )

    result = drift_module.validate_node(stub, entry_points=set(), strict=True)

    assert result.passed is True
    assert result.findings == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "name: runtime_node\ncapabilities:\n  full_runtime: true\n",
        "name: runtime_node\ncapabilities:\n  standalone: true\n",
    ],
)
def test_contractless_node_not_declared_stub_still_fails(
    drift_module: ModuleType, tmp_path: Path, metadata: str | None
) -> None:
    """Only an explicit full_runtime false exempts a node from the contract check."""
    node = tmp_path / "node_runtime"
    node.mkdir()
    if metadata is not None:
        (node / "metadata.yaml").write_text(metadata)

    result = drift_module.validate_node(node, entry_points=set(), strict=False)

    assert result.passed is False
    assert [f.check for f in result.findings] == ["contract_exists"]
