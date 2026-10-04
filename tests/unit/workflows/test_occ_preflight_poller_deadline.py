# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Repo evidence pollers outwait the pinned verifier's 40-minute timeout."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
VERIFIER_TIMEOUT_SECONDS = 40 * 60
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
def test_poller_deadline_outwaits_the_repo_evidence_verifier(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    deadlines = [int(d) for d in re.findall(r"^\s*deadline=(\d+)$", text, re.M)]
    assert len(deadlines) == 1, f"{name} must carry exactly one poller deadline"
    assert deadlines[0] > VERIFIER_TIMEOUT_SECONDS
