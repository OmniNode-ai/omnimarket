# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20032: the unbound-criterion refusal test runs in CI and in pre-commit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_GUARD_TEST = "tests/test_dod_verify_every_criterion_bound.py"
_HOOK_ID = "dod-verify-every-criterion-bound"


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


def test_pre_commit_hook_runs_the_guarding_test_check_only() -> None:
    hook = _hook()
    assert _GUARD_TEST in hook["entry"]
    assert hook["entry"].startswith("uv run pytest ")
    assert hook["pass_filenames"] is False
    assert "pre-commit" in hook["stages"]


def test_required_lint_job_runs_the_guarding_test() -> None:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    runs = [str(step.get("run", "")) for step in workflow["jobs"]["lint"]["steps"]]
    assert any(_GUARD_TEST in run and "pytest" in run for run in runs)


def test_lint_is_a_required_check() -> None:
    required = yaml.safe_load(
        (_ROOT / ".github" / "required-checks.yaml").read_text("utf-8")
    )
    names = {
        entry["name"]
        for entry in _entries(required)
        if isinstance(entry, dict) and "name" in entry
    }
    assert "lint" in names


def _entries(node: Any) -> list[Any]:
    if isinstance(node, list):
        return [item for entry in node for item in [entry, *_entries(entry)]]
    if isinstance(node, dict):
        return [item for value in node.values() for item in _entries(value)]
    return []
