# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20780: the typed-decision repository-scope tests run in CI and in pre-commit.

Operator rulings 2026-10-13T18:34:53Z and 2026-10-13T20:50:03Z: a typed decision may
be asked for a private code repository; only the repositories on the contract's
refused list are refused. The node's handler tests pin that, and this file fails when
the required lint job or the check-only hook stops naming them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_GUARD_TEST = (
    "tests/unit/nodes/node_typed_decision_effect/test_handler_typed_decision.py"
)
_HOOK_ID = "typed-decision-repository-scope"


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


@pytest.mark.parametrize(
    "path",
    [
        _GUARD_TEST,
        "src/omnimarket/nodes/node_typed_decision_effect/contract.yaml",
        "src/omnimarket/nodes/node_typed_decision_effect/handlers/handler_typed_decision.py",
    ],
)
def test_hook_files_filter_selects_the_scope_sources(path: str) -> None:
    assert re.search(_hook()["files"], path)
