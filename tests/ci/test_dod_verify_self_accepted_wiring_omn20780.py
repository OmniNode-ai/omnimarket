# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20780: the self-accepted binding tests run in CI and in pre-commit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_GUARD_TESTS = (
    "tests/test_occ_falsifier_checks.py",
    "tests/test_dod_verify_acceptance_basis.py",
)
_HOOK_ID = "dod-verify-self-accepted-bindings"


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


@pytest.mark.parametrize("guard_test", _GUARD_TESTS)
def test_pre_commit_hook_runs_the_guarding_tests_check_only(guard_test: str) -> None:
    hook = _hook()
    assert guard_test in hook["entry"]
    assert hook["entry"].startswith("uv run pytest ")
    assert hook["pass_filenames"] is False
    assert "pre-commit" in hook["stages"]


@pytest.mark.parametrize("guard_test", _GUARD_TESTS)
def test_required_lint_job_runs_the_guarding_tests(guard_test: str) -> None:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    runs = [str(step.get("run", "")) for step in workflow["jobs"]["lint"]["steps"]]
    assert any(guard_test in run and "pytest" in run for run in runs)


@pytest.mark.parametrize("guard_test", _GUARD_TESTS)
def test_hook_files_filter_selects_the_guarding_test(guard_test: str) -> None:
    import re

    assert re.search(_hook()["files"], guard_test)
