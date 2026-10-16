# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18925 AC-2: the schema-invalid-terminal check is a pre-merge gate, not only a test.

The invariant lives in ``test_schema_invalid_terminal_never_passes_omn18925.py``.
This file fails the build when the lint job step or the pre-commit hook that
runs it is removed, renamed away from that file, or no longer a required check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_INVARIANT_TEST = (
    "tests/unit/delegation/test_schema_invalid_terminal_never_passes_omn18925.py"
)
_WIRING_TEST = "tests/ci/test_schema_invalid_terminal_gate_wiring_omn18925.py"
_HOOK_ID = "schema-invalid-terminal-never-passes"


def _hooks() -> list[dict[str, Any]]:
    config = yaml.safe_load((_ROOT / ".pre-commit-config.yaml").read_text("utf-8"))
    return [
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == _HOOK_ID
    ]


def _lint_runs() -> list[str]:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    return [str(step.get("run", "")) for step in workflow["jobs"]["lint"]["steps"]]


def _names(node: Any) -> set[str]:
    if isinstance(node, dict):
        found = {str(node["name"])} if "name" in node else set()
        for value in node.values():
            found |= _names(value)
        return found
    if isinstance(node, list):
        return set().union(*(_names(item) for item in node)) if node else set()
    return set()


def test_pre_commit_hook_runs_the_invariant_and_this_wiring_check_only() -> None:
    hooks = _hooks()
    assert len(hooks) == 1
    hook = hooks[0]
    assert _INVARIANT_TEST in hook["entry"]
    assert _WIRING_TEST in hook["entry"]
    assert "pytest" in hook["entry"]
    assert hook["pass_filenames"] is False
    assert "pre-commit" in hook["stages"]


def test_required_lint_job_runs_the_invariant_and_this_wiring_check() -> None:
    runs = [run for run in _lint_runs() if _INVARIANT_TEST in run]
    assert len(runs) == 1
    assert _WIRING_TEST in runs[0]
    assert "pytest" in runs[0]


def test_the_gated_files_exist() -> None:
    assert (_ROOT / _INVARIANT_TEST).is_file()
    assert (_ROOT / _WIRING_TEST).is_file()


def test_lint_is_a_required_check() -> None:
    required = yaml.safe_load(
        (_ROOT / ".github" / "required-checks.yaml").read_text("utf-8")
    )
    assert "lint" in _names(required)
