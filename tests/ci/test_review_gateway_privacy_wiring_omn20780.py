# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20780: the review gateway privacy tests run in CI and in pre-commit.

Operator ruling 2026-10-13T20:50:03Z: a cloud review is refused for the one refused
repository and served for every other. The gateway tests pin that, and this file
fails when the required lint job or the check-only hook stops naming them.
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
    "tests/unit/nodes/node_claude_review_gateway_effect/test_claude_review_gateway.py"
)
_HOOK_ID = "review-gateway-privacy"


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


def test_privacy_hook_runs_the_guarding_test_check_only() -> None:
    hook = _hook()
    entry = hook["entry"]
    assert entry.startswith("uv run pytest ")
    assert _GUARD_TEST in entry
    assert "-k privacy" in entry
    assert hook["pass_filenames"] is False
    assert "pre-commit" in hook["stages"]
    assert "--fix" not in entry
    assert "--write" not in entry


def test_privacy_lint_step_runs_the_guarding_test() -> None:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    steps = [
        step
        for step in workflow["jobs"]["lint"]["steps"]
        if _GUARD_TEST in str(step.get("run", ""))
        and "pytest" in str(step.get("run", ""))
        and "-k privacy" in str(step.get("run", ""))
    ]
    assert steps
    assert all("continue-on-error" not in step for step in steps)


@pytest.mark.parametrize(
    "path",
    [
        _GUARD_TEST,
        "src/omnimarket/nodes/node_claude_review_gateway_effect/contract.yaml",
        "src/omnimarket/nodes/node_claude_review_gateway_effect/handlers/"
        "handler_claude_review_gateway.py",
        "src/omnimarket/nodes/node_claude_review_gateway_effect/models/"
        "model_claude_review_gateway.py",
        "tests/ci/test_review_gateway_privacy_wiring_omn20780.py",
    ],
)
def test_privacy_hook_files_filter_selects_the_privacy_sources(path: str) -> None:
    assert re.search(_hook()["files"], path)
