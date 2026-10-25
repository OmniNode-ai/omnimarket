# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep the harness invocation check wired into CI and pre-commit (OMN-21289)."""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMAND = "uv run python -m omnimarket.nodes.node_harness_invocation_check_effect"


@pytest.mark.unit
def test_harness_invocation_check_runs_in_lint() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = [
        step
        for step in workflow["jobs"]["lint"]["steps"]
        if step.get("name") == "Harness invocation gate"
    ]
    assert len(steps) == 1
    assert steps[0]["run"] == COMMAND


@pytest.mark.unit
def test_harness_invocation_check_runs_in_precommit() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    hooks = [
        hook
        for repo in config["repos"]
        if repo["repo"] == "local"
        for hook in repo["hooks"]
        if hook["id"] == "harness-invocation-check"
    ]
    assert len(hooks) == 1
    assert hooks[0]["entry"] == COMMAND
    assert hooks[0]["language"] == "system"
    assert hooks[0]["pass_filenames"] is False
