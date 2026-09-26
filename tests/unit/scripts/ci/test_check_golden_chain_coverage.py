# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19714 tests for golden-chain error-leg coverage."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_golden_chain_coverage.py"
PLANTED_NODE = "node_planted_orchestrator"
WORKFLOW_OWNER = "node_planted_orchestrator"


def _git(args: list[str], cwd: Path) -> None:
    """Run git without inheriting hook-owned repository location variables."""
    subprocess.run(args, cwd=cwd, check=True, env=scrub_git_location_env())


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "check_golden_chain_coverage_omn_19714", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def coverage_module() -> object:
    return _load_module()


def _write_node(repo_root: Path, node_name: str) -> None:
    node_dir = repo_root / "src" / "omnimarket" / "nodes" / node_name
    node_dir.mkdir(parents=True)
    suffix = node_name.removeprefix("node_")
    class_name = "".join(part.title() for part in suffix.split("_"))
    (node_dir / "contract.yaml").write_text(
        "\n".join(
            [
                f"name: {node_name}",
                "handler:",
                f"  module: omnimarket.nodes.{node_name}.handler_{suffix}",
                f"  class: Handler{class_name}",
                "",
            ]
        )
    )


def _write_golden_chain_test(repo_root: Path, node_name: str) -> None:
    tests_dir = repo_root / "tests"
    tests_dir.mkdir(exist_ok=True)
    suffix = node_name.removeprefix("node_")
    (tests_dir / f"test_golden_chain_{suffix}.py").write_text(
        f"# Golden-chain coverage for {node_name}\n"
    )


def _write_walker_report(
    repo_root: Path,
    *,
    components: list[str] | None = None,
    path_kinds: tuple[str, ...] = ("error",),
) -> Path:
    report_path = repo_root / "tests" / "chains" / "planted" / "walker_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflow_owner": WORKFLOW_OWNER,
                "components": components or [PLANTED_NODE, "node_planted_reducer"],
                "core_sha": "a" * 40,
                "paths": [
                    {
                        "path_id": f"{kind}:trigger_a>trigger_b",
                        "kind": kind,
                        "steps": [[PLANTED_NODE, "trigger_a", "node_planted_reducer"]],
                    }
                    for kind in path_kinds
                ],
            }
        )
    )
    return report_path


def _run_check_all(coverage_module: object, *, output_json: bool = False) -> int:
    return coverage_module.run(  # type: ignore[attr-defined]
        changed_ref=None,
        staged=False,
        check_all=True,
        output_json=output_json,
    )


def _plant_covered_node(repo_root: Path) -> None:
    _write_node(repo_root, PLANTED_NODE)
    _write_golden_chain_test(repo_root, PLANTED_NODE)


def test_error_path_without_error_chain_case_fails(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plant_covered_node(tmp_path)
    _write_walker_report(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert _run_check_all(coverage_module) == 1
    assert (
        "error_leg: changed node node_planted_orchestrator" in capsys.readouterr().out
    )


def test_error_chain_case_for_node_passes(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plant_covered_node(tmp_path)
    _write_walker_report(tmp_path)
    chain_test = tmp_path / "tests" / "chains" / "planted" / "test_chain_planted.py"
    chain_test.write_text(
        "def test_planted_error_chain():\n"
        "    assert_error_chain(node_planted_orchestrator)\n"
    )
    monkeypatch.chdir(tmp_path)

    assert _run_check_all(coverage_module) == 0


def test_happy_path_assertion_does_not_cover_error_leg(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plant_covered_node(tmp_path)
    _write_walker_report(tmp_path)
    chain_test = tmp_path / "tests" / "chains" / "planted" / "test_chain_planted.py"
    chain_test.write_text(
        "def test_planted_golden_chain():\n"
        "    assert_chain(node_planted_orchestrator)\n"
    )
    monkeypatch.chdir(tmp_path)

    assert _run_check_all(coverage_module) == 1
    assert "error_leg" in capsys.readouterr().out


def test_node_without_walker_report_is_explicitly_not_applicable(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plant_covered_node(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert _run_check_all(coverage_module) == 0
    assert (
        "  [SKIP error_leg] node_planted_orchestrator: no walker report names this node"
    ) in capsys.readouterr().out

    assert _run_check_all(coverage_module, output_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["error_leg"] == "not_applicable_no_walker_report"


def test_walker_report_without_error_paths_is_not_applicable(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plant_covered_node(tmp_path)
    _write_walker_report(tmp_path, path_kinds=("golden",))
    monkeypatch.chdir(tmp_path)

    assert _run_check_all(coverage_module, output_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["error_leg"] == "not_applicable_no_error_paths"


def test_corrupt_walker_report_fails_and_names_file(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plant_covered_node(tmp_path)
    report_path = tmp_path / "tests" / "chains" / "planted" / "walker_report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("{not valid json")
    monkeypatch.chdir(tmp_path)

    assert _run_check_all(coverage_module) == 1
    captured = capsys.readouterr()
    assert "tests/chains/planted/walker_report.json" in captured.out + captured.err


def test_changed_ref_does_not_enforce_unchanged_node_gap(
    coverage_module: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plant_covered_node(tmp_path)
    _write_walker_report(tmp_path)
    unrelated_node = "node_unrelated_effect"
    _write_node(tmp_path, unrelated_node)
    _write_golden_chain_test(tmp_path, unrelated_node)

    _git(["git", "init", "-q"], tmp_path)
    _git(["git", "config", "user.email", "test@example.com"], tmp_path)
    _git(["git", "config", "user.name", "Test User"], tmp_path)
    _git(["git", "add", "."], tmp_path)
    _git(["git", "commit", "-q", "-m", "base"], tmp_path)
    _git(["git", "branch", "base"], tmp_path)
    _git(["git", "switch", "-q", "-c", "feature"], tmp_path)
    unrelated_contract = (
        tmp_path / "src" / "omnimarket" / "nodes" / unrelated_node / "contract.yaml"
    )
    unrelated_contract.write_text(
        unrelated_contract.read_text() + "description: changed\n"
    )
    _git(["git", "add", "."], tmp_path)
    _git(["git", "commit", "-q", "-m", "change unrelated node"], tmp_path)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        # The gate shells out to git diff with the process env; a hook-exported
        # location variable would retarget it at the real worktree.
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    assert (
        coverage_module.run(  # type: ignore[attr-defined]
            changed_ref="base",
            staged=False,
            check_all=False,
            output_json=False,
        )
        == 0
    )
