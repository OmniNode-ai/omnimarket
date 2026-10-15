# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20780: the delegated-answer gate tests run in CI and in pre-commit (ruling 2026-10-14T18:17:30Z)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_GATE_TEST = "tests/unit/delegation/test_answer_gate_omn20489.py"
_HOOK_ID = "delegation-answer-gate"


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


def test_pre_commit_hook_runs_the_gate_tests_check_only() -> None:
    hook = _hook()
    assert _GATE_TEST in hook["entry"]
    assert hook["entry"].startswith("uv run --frozen pytest ")
    assert hook["pass_filenames"] is False
    assert "pre-commit" in hook["stages"]


def test_required_lint_job_runs_the_gate_tests() -> None:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    runs = [str(step.get("run", "")) for step in workflow["jobs"]["lint"]["steps"]]
    assert any(_GATE_TEST in run and "pytest" in run for run in runs)


def test_hook_files_filter_selects_the_gate_test_and_its_handler() -> None:
    pattern = _hook()["files"]
    assert re.search(pattern, _GATE_TEST)
    handler = (
        "src/omnimarket/nodes/node_delegation_answer_gate_compute/handlers/"
        "handler_delegation_answer_gate.py"
    )
    assert (_ROOT / handler).is_file()
    assert re.search(pattern, handler)
