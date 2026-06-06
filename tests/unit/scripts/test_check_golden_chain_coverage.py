# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/ci/check_golden_chain_coverage.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_golden_chain_coverage.py"


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "check_golden_chain_coverage", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_golden_chain_coverage"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def coverage_module() -> object:
    return _load_module()


def _write_node(
    repo_root: Path,
    node_name: str,
    *,
    deprecated: bool = False,
    full_runtime: bool = True,
) -> Path:
    node_dir = repo_root / "src" / "omnimarket" / "nodes" / node_name
    node_dir.mkdir(parents=True)
    suffix = node_name.removeprefix("node_")
    class_name = "".join(part.title() for part in suffix.split("_"))
    (node_dir / "contract.yaml").write_text(
        "\n".join(
            [
                f"name: {node_name}",
                "node_type: compute",
                "handler:",
                f"  module: omnimarket.nodes.{node_name}.handlers.handler_{suffix}",
                f"  class: Handler{class_name}",
                "",
            ]
        )
    )
    (node_dir / "metadata.yaml").write_text(
        "\n".join(
            [
                f"name: {node_name}",
                'version: "1.0.0"',
                f"description: {node_name}",
                "capabilities:",
                f"  full_runtime: {str(full_runtime).lower()}",
                f"deprecated: {str(deprecated).lower()}",
                "",
            ]
        )
    )
    return node_dir


@pytest.mark.unit
def test_changed_active_node_without_golden_chain_fails(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed live-path node cannot ship without a matching golden-chain test."""
    _write_node(tmp_path, "node_alpha_compute")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        coverage_module,
        "_changed_files_from_ref",
        lambda _ref: [
            "src/omnimarket/nodes/node_alpha_compute/handlers/handler_alpha_compute.py"
        ],
    )

    assert (
        coverage_module.run(  # type: ignore[attr-defined]
            changed_ref="origin/dev",
            staged=False,
            check_all=False,
            output_json=False,
        )
        == 1
    )


@pytest.mark.unit
def test_repo_level_golden_chain_reference_satisfies_gate(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repo-level golden-chain tests count when they reference the node module."""
    _write_node(tmp_path, "node_alpha_compute")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_golden_chain_alpha_compute.py").write_text(
        "from omnimarket.nodes.node_alpha_compute.handlers.handler_alpha_compute "
        "import HandlerAlphaCompute\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        coverage_module,
        "_changed_files_from_ref",
        lambda _ref: ["src/omnimarket/nodes/node_alpha_compute/models/model_alpha.py"],
    )

    assert (
        coverage_module.run(  # type: ignore[attr-defined]
            changed_ref="origin/dev",
            staged=False,
            check_all=False,
            output_json=False,
        )
        == 0
    )


@pytest.mark.unit
def test_node_local_golden_chain_satisfies_gate(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Node-local golden-chain tests count by path even without content references."""
    node_dir = _write_node(tmp_path, "node_beta_effect")
    local_tests_dir = node_dir / "tests"
    local_tests_dir.mkdir()
    (local_tests_dir / "test_golden_chain.py").write_text("def test_beta(): pass\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        coverage_module,
        "_changed_files_from_staged",
        lambda: ["src/omnimarket/nodes/node_beta_effect/contract.yaml"],
    )

    assert (
        coverage_module.run(  # type: ignore[attr-defined]
            changed_ref=None,
            staged=True,
            check_all=False,
            output_json=False,
        )
        == 0
    )


@pytest.mark.unit
def test_deprecated_node_is_not_enforced(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deprecated nodes are not live-path enforcement targets."""
    _write_node(tmp_path, "node_legacy_effect", deprecated=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        coverage_module,
        "_changed_files_from_ref",
        lambda _ref: ["src/omnimarket/nodes/node_legacy_effect/contract.yaml"],
    )

    assert (
        coverage_module.run(  # type: ignore[attr-defined]
            changed_ref="origin/dev",
            staged=False,
            check_all=False,
            output_json=False,
        )
        == 0
    )


@pytest.mark.unit
def test_non_node_change_has_no_targets(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-node changes should not trigger a broad baseline audit."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        coverage_module,
        "_changed_files_from_ref",
        lambda _ref: ["docs/integration-plan.md"],
    )

    assert (
        coverage_module.run(  # type: ignore[attr-defined]
            changed_ref="origin/dev",
            staged=False,
            check_all=False,
            output_json=False,
        )
        == 0
    )
