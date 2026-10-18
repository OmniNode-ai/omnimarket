# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17427: the verifier-floor test of the repo-evidence caller runs in CI and in pre-commit.

The hook and the lint step run the same command under the same name, so neither can
drift from the other, and the hook line names the caller workflow's stem so the
ruling-mechanism audit finds a pre-commit hook that references it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_FLOOR_TEST = "tests/ci/test_repo_evidence_caller_verifier_floor_omn20032.py"
_CALLER = ".github/workflows/call-repo-evidence-gate.yml"
_HOOK_ID = "call-repo-evidence-gate-verifier-floor"
_STEP_NAME = "call-repo-evidence-gate refuses a verifier below the floor (OMN-17427)"


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


def _lint_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    steps: list[dict[str, Any]] = workflow["jobs"]["lint"]["steps"]
    return steps


def test_pre_commit_hook_runs_the_floor_test_check_only() -> None:
    hook = _hook()
    assert _FLOOR_TEST in hook["entry"]
    assert hook["entry"].startswith("uv run pytest ")
    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False
    assert hook["stages"] == ["pre-commit"]


def test_hook_is_scoped_to_the_caller_the_test_and_the_config() -> None:
    value = _hook()["files"]
    assert isinstance(value, str)
    files = re.compile(value)
    for path in (_CALLER, _FLOOR_TEST, ".pre-commit-config.yaml"):
        assert files.search(path), path
    assert not files.search("src/omnimarket/nodes/node_dod_verify/handler.py")


def test_hook_line_names_the_caller_workflow_stem() -> None:
    # The audit greps the non-files:/non-exclude: lines of .pre-commit-config.yaml
    # for the workflow's whole stem; the hook id continues past the stem, so the
    # name is the line that carries it.
    stem = Path(_CALLER).stem
    needle = re.compile(r"(?<![\w./-])" + re.escape(stem) + r"(?![\w./-])")
    lines = [
        line
        for line in (_ROOT / ".pre-commit-config.yaml").read_text("utf-8").splitlines()
        if not line.lstrip().startswith("#")
        and not re.match(r"^\s*(?:-\s*)?(?:files|exclude):", line)
    ]
    assert any(needle.search(line) for line in lines)
    assert needle.search(_hook()["name"])


def test_required_lint_job_runs_the_same_command_under_the_same_name() -> None:
    hook = _hook()
    steps = [step for step in _lint_steps() if step.get("name") == _STEP_NAME]
    assert len(steps) == 1
    assert hook["name"] == _STEP_NAME
    assert steps[0]["run"].strip() == hook["entry"].strip()


def _comment_prose(text: str) -> str:
    """Comment lines joined with their `#` markers dropped, so a wrapped sentence matches."""
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("#")]
    return " ".join(line.removeprefix("#").strip() for line in lines)


def test_lint_step_and_hook_cite_each_other() -> None:
    ci_text = (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    config_text = (_ROOT / ".pre-commit-config.yaml").read_text("utf-8")
    assert f"SYNC with pre-commit hook '{_HOOK_ID}'" in _comment_prose(ci_text)
    assert f'SYNC with the ci.yml lint step "{_STEP_NAME}"' in _comment_prose(
        config_text
    )


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
