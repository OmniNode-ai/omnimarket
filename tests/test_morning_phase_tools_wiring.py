# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep the existing morning tools guard on every commit and required CI run."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GUARD = (
    "tests/nodes/node_morning_ground_state_orchestrator/"
    "test_morning_phase_tools_guard.py"
)
WIRING = "tests/test_morning_phase_tools_wiring.py"


@pytest.mark.unit
def test_morning_phase_tools_wiring_pre_commit() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [
        hook
        for repo in config["repos"]
        if repo["repo"] == "local"
        for hook in repo["hooks"]
        if GUARD in shlex.split(hook.get("entry", ""))
    ]
    assert hooks, "pre-commit must run the existing morning phase tools guard"
    for hook in hooks:
        assert WIRING in shlex.split(hook["entry"])
        assert hook["always_run"] is True
        assert hook["pass_filenames"] is False
        assert hook["stages"] == ["pre-commit"]
        assert "files" not in hook
        assert "exclude" not in hook
    assert not config.get("files")
    assert not config.get("exclude")


@pytest.mark.unit
def test_morning_phase_tools_wiring_required_ci() -> None:
    config = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    # lint is an existing strict gate of CI Summary, including docs-only PRs.
    job = config["jobs"]["lint"]
    assert job.get("if") in (None, "always()")
    assert "paths" not in job
    assert "paths-ignore" not in job
    assert not job.get("continue-on-error", False)
    events = config.get("on", config.get(True))
    for event in ("pull_request", "push"):
        assert "paths" not in (events[event] or {})
        assert "paths-ignore" not in (events[event] or {})
    steps = [step for step in job["steps"] if GUARD in step.get("run", "")]
    assert steps, "required CI lint job must run the existing morning phase tools guard"
    for step in steps:
        assert GUARD in shlex.split(step["run"])
        assert WIRING in shlex.split(step["run"])
        assert "if" not in step
        assert not step.get("continue-on-error", False)
