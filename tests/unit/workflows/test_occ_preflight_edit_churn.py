# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20038: repo-evidence pollers wait for a newer run after cancellation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
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
def test_poller_cancelled_is_pending_only_with_a_newer_run(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert re.search(r"failure\|cancelled\|", text) is None
    assert re.search(r"^\s+cancelled\)\n", text, re.M)
    assert "check_name=repo-evidence%20%2F%20dod-verify" in text
    assert ".app.id == 15368" in text
    assert "sort_by(.id)" in text
    # Fail closed: a read error or a zero count refuses.
    assert "|| in_flight=0" in text
    cancelled_block = text.split("cancelled)\n", 1)[1].split(";;", 1)[0]
    assert "exit 1" in cancelled_block
    gh_line = next(ln for ln in cancelled_block.splitlines() if "gh api" in ln)
    assert "2>/dev/null" not in gh_line
    jobs = yaml.safe_load(text)["jobs"]
    poller = next(
        j for j in jobs.values() if j.get("name") == "Repo Evidence Dependency"
    )
    assert poller["permissions"]["actions"] == "read"
