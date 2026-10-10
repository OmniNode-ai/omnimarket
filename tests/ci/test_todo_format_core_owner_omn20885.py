# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20885: the lint job's ``TODO format check`` reads no onex_change_control.

The step began with a best-effort install of the change-control package whose
result was discarded; the inline ``find | grep`` loop that followed never read
it. The step now runs ``omnibase_core``'s ``handler_todo_format`` -- the same
handler the ``no-untracked-todos`` pre-commit hook runs -- over the ``src``
tree the job already installed it for, under the unchanged step name.

These tests run the step's own ``run`` block, extracted from ``ci.yml``, in
temporary trees: a clean one passes and a planted untracked marker fails
naming the file and line.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JOB_KEY = "lint"
STEP_NAME = "TODO format check"

_MARKER = "TO" + "DO"
_CLEAN = f"x = 1\n# {_MARKER}(OMN-1): tracked\n"
_BARE = f"x = 1\n# {_MARKER} fix this\n"


def _step() -> dict[str, object]:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    steps = jobs[JOB_KEY]["steps"]
    (step,) = [step for step in steps if step.get("name") == STEP_NAME]
    return step


def _run_block() -> str:
    run = _step()["run"]
    assert isinstance(run, str)
    return run


def _run_step(cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["UV_PROJECT"] = str(REPO_ROOT)
    env["UV_FROZEN"] = "1"
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _run_block()],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_step_names_no_onex_change_control() -> None:
    rendered = yaml.safe_dump(_step())
    assert "onex_change_control" not in rendered
    assert "onex-change-control" not in rendered
    assert "pip install" not in rendered
    assert "|| true" not in rendered


def test_step_runs_the_core_handler() -> None:
    assert "omnibase_core.handlers.handler_todo_format" in _run_block()


def test_clean_tree_passes(tmp_path: Path) -> None:
    result = _run_step(_tree(tmp_path, {"src/pkg/mod.py": _CLEAN}))
    assert result.returncode == 0, result.stdout + result.stderr


def test_planted_untracked_marker_fails_naming_file_and_line(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"src/pkg/ok.py": _CLEAN, "src/pkg/mod.py": _BARE})
    result = _run_step(root)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "src/pkg/mod.py:2:" in result.stdout
    assert "src/pkg/ok.py" not in result.stdout


def test_planted_marker_in_nested_package_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"src/a/b/c/deep.py": f"# FIX{'ME'}\n"})
    result = _run_step(root)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "src/a/b/c/deep.py:1:" in result.stdout


def test_marker_under_a_tests_directory_is_not_scanned(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"src/pkg/tests/test_x.py": _BARE})
    result = _run_step(root)
    assert result.returncode == 0, result.stdout + result.stderr
