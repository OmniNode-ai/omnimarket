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
import sys
from pathlib import Path

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
def test_validate_node_strict_only_on_directly_modified(
    drift_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing-on-main violation MUST stay WARN unless the node itself was modified."""
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
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
