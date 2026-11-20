# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The latest-core smoke names a symbol a core build no longer ships, offline.

Fixture (AC3): tests/ci/fixtures/latest_core_smoke/node_run_node_user/cli.py imports
``run_node_in_process`` from ``omnibase_core.cli.cli_run_node`` exactly as
node_onex_setup_effect/cli.py did when core removed it. The fake core is built in a
temp directory, so no network and no real core install is involved.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / "scripts" / "ci" / "latest_core_smoke.py"
FIXTURES = Path(__file__).parent / "fixtures" / "latest_core_smoke"
PACKAGE = "node_run_node_user"


def _fake_core(root: Path, *, symbols: tuple[str, ...]) -> Path:
    """Build a fake omnibase_core whose cli_run_node module defines only ``symbols``."""
    cli = root / "omnibase_core" / "cli"
    cli.mkdir(parents=True)
    (root / "omnibase_core" / "__init__.py").write_text("")
    (cli / "__init__.py").write_text("")
    body = "".join(
        f"def {name}(*args, **kwargs):\n    return None\n\n" for name in symbols
    )
    (cli / "cli_run_node.py").write_text(textwrap.dedent(body) or "# empty\n")
    return root


def _run_smoke(core_root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join([str(core_root), str(FIXTURES)]),
    }
    return subprocess.run(
        [sys.executable, str(SMOKE), "--module", PACKAGE, *extra],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def test_synthetic_removal_makes_the_import_step_fail(tmp_path: Path) -> None:
    """A fake core build that lacks a symbol the node imports fails the smoke."""
    core = _fake_core(tmp_path / "core", symbols=("some_other_helper",))
    result = _run_smoke(core)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "LATEST-CORE-SMOKE FAIL" in result.stdout
    assert f"module={PACKAGE}.cli" in result.stdout


def test_synthetic_removal_control_passes_when_symbol_is_present(
    tmp_path: Path,
) -> None:
    """Positive control: the same fake core that keeps the symbol passes."""
    core = _fake_core(tmp_path / "core", symbols=("run_node_in_process",))
    result = _run_smoke(core)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "failed=0" in result.stdout


def test_run_node_in_process_absent_is_named_and_exits_nonzero(tmp_path: Path) -> None:
    core = _fake_core(tmp_path / "core", symbols=())
    result = _run_smoke(core)
    assert result.returncode != 0
    assert "symbol=omnibase_core.cli.cli_run_node.run_node_in_process" in result.stdout


def test_run_node_in_process_present_passes(tmp_path: Path) -> None:
    core = _fake_core(tmp_path / "core", symbols=("run_node_in_process",))
    result = _run_smoke(core)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "modules_tried=2" in result.stdout


def test_no_walk_imports_only_the_package_so_the_cli_import_is_not_seen(
    tmp_path: Path,
) -> None:
    """Why the smoke walks submodules: a package-only import misses the incident."""
    core = _fake_core(tmp_path / "core", symbols=())
    result = _run_smoke(core, "--no-walk")
    assert result.returncode == 0


def test_empty_import_set_is_not_a_pass(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SMOKE), "--group", "onex.nodes.no_such_group"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        check=False,
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr


@pytest.mark.parametrize("path", [SMOKE])
def test_smoke_script_is_stdlib_only(path: Path) -> None:
    text = path.read_text()
    assert "import omnibase" not in text
    assert "import omnimarket" not in text


WORKFLOWS = REPO / ".github" / "workflows"


def test_ci_declares_the_latest_core_smoke_job_strict_and_required() -> None:
    """AC1: ci.yml has the job, the summary rules list it, the manifest requires it."""
    assert "latest-core-smoke:" in (WORKFLOWS / "ci.yml").read_text()
    rules = (REPO / "scripts" / "ci" / "ci_summary_rules.json").read_text()
    assert '"Latest Core Smoke"' in rules
    required = (REPO / ".github" / "required-checks.yaml").read_text()
    assert 'name: "Latest Core Smoke"' in required


def test_reusable_workflow_is_callable_from_a_core_pr() -> None:
    """AC4 precondition: the reusable workflow exists and is a workflow_call."""
    text = (WORKFLOWS / "latest-core-smoke-reusable.yml").read_text()
    assert "workflow_call" in text
    assert "scripts/ci/latest_core_smoke.py" in text
