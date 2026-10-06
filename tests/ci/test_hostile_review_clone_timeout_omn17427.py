# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the hostile reviewer's sibling clone steps get a fetch budget that fits.

On 2026-10-05 from about 09:25Z every Hostile Reviewer run that cloned a
sibling repository died at the step "Clone omnibase_core" with "failed after 4
attempts": the verdict was empty, the review never started and the gate failed
as ``unknown`` with zero findings. Each attempt ended exactly 20 seconds after
it started, because the step wrapped ``git fetch --depth=1`` in ``timeout 20s``
while a fetch of the pinned tag took far longer than that on the runners (one
measured fetch took about 110 seconds, so a 120 second budget passed with 10
seconds to spare, and the budget is now 240).

A budget that is shorter than a slow GitHub fetch turns a network slowdown into
a red gate on every pull request at once, so the three sibling clone steps may
never go back under the measured floor.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "hostile-reviewer.yml"
)
SIBLINGS = ("omniintelligence", "omnibase_core", "omnibase_compat")
# The fetch that took about 110 seconds sets the floor, with a margin above it.
MIN_ATTEMPT_BUDGET_SECONDS = 120
_TIMEOUT = re.compile(r"timeout\s+(\d+)s\s+bash\s+-c\s+'git init")


def _clone_steps() -> list[dict[str, Any]]:
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = parsed["jobs"]["hostile-review"]["steps"]
    assert isinstance(steps, list)
    return [s for s in steps if "clone_with_retry" in str(s.get("run", ""))]


def test_every_sibling_repository_has_a_clone_step() -> None:
    """The check below must not pass vacuously on a workflow with no clone steps."""
    runs = " ".join(str(s.get("run", "")) for s in _clone_steps())
    for repo in SIBLINGS:
        assert re.search(rf"clone_with_retry\s+{repo}\s", runs), (
            f"no clone_with_retry call for {repo} in hostile-reviewer.yml"
        )


def test_each_clone_attempt_budget_is_at_least_the_measured_floor() -> None:
    """A per-attempt timeout under the floor kills a slow but healthy fetch."""
    steps = _clone_steps()
    assert len(steps) >= len(SIBLINGS)
    for step in steps:
        budgets = [int(m) for m in _TIMEOUT.findall(str(step["run"]))]
        assert budgets, f"step {step.get('name')!r} has no per-attempt timeout"
        for budget in budgets:
            assert budget >= MIN_ATTEMPT_BUDGET_SECONDS, (
                f"step {step.get('name')!r} gives each clone attempt {budget}s, "
                f"under the {MIN_ATTEMPT_BUDGET_SECONDS}s floor (OMN-17427)"
            )
