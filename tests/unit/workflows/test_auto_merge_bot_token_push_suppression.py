# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16081: auto-merge must not arm under the default GITHUB_TOKEN.

Live-verified 2026-08-15: an auto-merge completed under `secrets.GITHUB_TOKEN`
is attributed to `github-actions[bot]`, and GitHub suppresses `push`-triggered
workflow runs on bot-actor commits as anti-recursion behavior. omnimarket#2078
merged 2026-08-15T22:04:46Z via this path and produced zero push-event
workflow runs on dev afterward.

Mirrors the OMN-15769 fix already proven in omninode_infra
(github.com/OmniNode-ai/omninode_infra/pull/830): only the "Enable auto-merge"
step's arming token moves to the org-wide `CROSS_REPO_PAT`. The token used by
`gh pr merge --auto` is what GitHub's deferred auto-merge completion later
attributes the squash-merge commit to; the enqueue/verify step does not
perform or complete a merge and is intentionally left untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
_AUTO_MERGE = _WORKFLOWS / "auto-merge.yml"


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _step(name: str) -> dict[str, Any]:
    steps = _load(_AUTO_MERGE)["jobs"]["auto-merge"]["steps"]
    matches = [s for s in steps if s.get("name") == name]
    assert len(matches) == 1, (
        f"expected exactly one step named {name!r}, found {[s.get('name') for s in steps]}"
    )
    return cast("dict[str, Any]", matches[0])


@pytest.mark.unit
def test_enable_auto_merge_step_arms_with_cross_repo_pat_not_github_token() -> None:
    """The step whose token attribution determines the merge-commit actor."""
    step = _step("Enable auto-merge")
    assert step["env"]["GH_TOKEN"] == "${{ secrets.CROSS_REPO_PAT }}", (
        "Enable auto-merge must arm gh pr merge --auto under CROSS_REPO_PAT, "
        "not GITHUB_TOKEN — a GITHUB_TOKEN-armed completion is attributed to "
        "github-actions[bot] and GitHub suppresses push-event workflow runs "
        "on bot-actor commits (OMN-16081, mirrors OMN-15769)."
    )


@pytest.mark.unit
def test_auto_merge_workflow_does_not_arm_the_merge_under_github_token() -> None:
    """Regression guard against re-introducing the exact starving line."""
    text = _AUTO_MERGE.read_text(encoding="utf-8")
    enable_block = text.split("- name: Enable auto-merge", 1)
    assert len(enable_block) == 2, "Enable auto-merge step not found in auto-merge.yml"
    # Only inspect this one step's body, up to the next step header.
    step_body = enable_block[1].split("\n      - name:", 1)[0]
    assert "secrets.GITHUB_TOKEN" not in step_body, (
        "Enable auto-merge step must not read the default GITHUB_TOKEN"
    )
    assert "secrets.CROSS_REPO_PAT" in step_body
