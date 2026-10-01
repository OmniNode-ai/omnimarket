# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The OCC Preflight Dependency pollers outwait the eligibility check they poll.

`occ-preflight / eligibility` waits up to 1500 s for its cited OCC companion to
merge, then fails closed. A poller whose own deadline is shorter gives up on an
eligibility run that is still waiting, fails its job, and skips the test shards
behind it, so the PR is red although eligibility later passes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
ELIGIBILITY_WAIT_SECONDS = 1500
POLLERS = (
    "auto-merge.yml",
    "ci.yml",
    "dep-health-gate.yml",
    "market-skill-baseline.yml",
    "plugin-compat-gate.yml",
    "validator-runtime-profiles.yml",
)


@pytest.mark.unit
@pytest.mark.parametrize("name", POLLERS)
def test_poller_deadline_outwaits_the_eligibility_check(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    deadlines = [int(d) for d in re.findall(r"^\s*deadline=(\d+)$", text, re.M)]
    assert len(deadlines) == 1, f"{name} must carry exactly one poller deadline"
    assert deadlines[0] > ELIGIBILITY_WAIT_SECONDS
