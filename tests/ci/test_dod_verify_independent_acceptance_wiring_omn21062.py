# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21062: the independent-acceptance cases run in CI and in pre-commit, one run per selector."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_TEST_FILE = "tests/unit/nodes/node_dod_verify/test_independent_acceptance.py"
_SELECTORS = (
    "self_accepted_is_proposal",
    "acceptance_without_rerun_refused",
    "independent_rerun_accepted",
)
_HOOK_ID = "dod-verify-independent-acceptance"


def _hook() -> dict[str, Any]:
    config = yaml.safe_load((_ROOT / ".pre-commit-config.yaml").read_text("utf-8"))
    hooks = [
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == _HOOK_ID
    ]
    assert len(hooks) == 1
    return hooks[0]


def _lint_runs() -> list[str]:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    return [str(step.get("run", "")) for step in workflow["jobs"]["lint"]["steps"]]


def test_pre_commit_hook_runs_every_selector_check_only() -> None:
    hook = _hook()
    assert _TEST_FILE in hook["entry"]
    assert all(selector in hook["entry"] for selector in _SELECTORS)
    assert '-k "$selector"' in hook["entry"]
    assert hook["pass_filenames"] is False
    assert "pre-commit" in hook["stages"]


def test_required_lint_job_runs_every_selector() -> None:
    runs = [run for run in _lint_runs() if _TEST_FILE in run]
    assert len(runs) == 1
    assert all(selector in runs[0] for selector in _SELECTORS)
    assert '-k "$selector"' in runs[0]
    assert "pytest" in runs[0]


def test_every_selector_names_a_case_in_the_test_file() -> None:
    source = (_ROOT / _TEST_FILE).read_text("utf-8")
    for selector in _SELECTORS:
        assert f"def test_{selector}" in source


def test_lint_is_a_required_check() -> None:
    required = yaml.safe_load(
        (_ROOT / ".github" / "required-checks.yaml").read_text("utf-8")
    )
    assert "lint" in {
        entry["name"]
        for entry in _entries(required)
        if isinstance(entry, dict) and "name" in entry
    }


def _entries(node: Any) -> list[Any]:
    if isinstance(node, list):
        return [item for entry in node for item in [entry, *_entries(entry)]]
    if isinstance(node, dict):
        return [item for value in node.values() for item in _entries(value)]
    return []
