# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The freshness run is wired to every push to dev and runs the node's own command (OMN-23537)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "test-duration-record-freshness.yml"
)


def _workflow() -> dict[str, object]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_run_triggers_on_every_push_to_dev_and_never_cancels() -> None:
    workflow = _workflow()
    triggers = (
        workflow[True] if True in workflow else workflow["on"]
    )  # PyYAML reads `on` as True
    assert isinstance(triggers, dict)
    assert triggers["push"] == {"branches": ["dev"]}
    concurrency = workflow["concurrency"]
    assert isinstance(concurrency, dict)
    assert concurrency["cancel-in-progress"] is False


def test_the_run_passes_the_push_before_and_head_to_the_node_command() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    steps = jobs["test-duration-record-freshness"]["steps"]
    run_steps = [step for step in steps if "run" in step]
    assert len(run_steps) == 1
    step = run_steps[0]
    command = " ".join(step["run"].split())
    assert "runtime_test_duration_record_check --freshness" in command
    assert '--before "$BEFORE_SHA" --head "$HEAD_SHA"' in command
    assert step["env"]["BEFORE_SHA"] == "${{ github.event.before }}"
    assert step["env"]["HEAD_SHA"] == "${{ github.sha }}"
    checkout = steps[0]
    assert checkout["with"]["fetch-depth"] == 0
