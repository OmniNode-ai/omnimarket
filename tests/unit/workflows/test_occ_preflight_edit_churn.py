# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20038: a PR-body edit must not kill an in-flight OCC Preflight run.

The caller keeps the `edited` trigger (the evidence stamp lands after the
event), so the guard is in the concurrency expression, and the OCC Preflight
Dependency pollers read a cancel as pending only while a newer run is queued.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
CALLER = WORKFLOWS / "call-occ-preflight.yml"
POLLERS = (
    "auto-merge.yml",
    "ci.yml",
    "dep-health-gate.yml",
    "market-skill-baseline.yml",
    "plugin-compat-gate.yml",
    "validator-runtime-profiles.yml",
)


@pytest.mark.unit
def test_concurrency_edited_never_cancels_in_flight() -> None:
    data = yaml.safe_load(CALLER.read_text(encoding="utf-8"))
    expr = str(data["concurrency"]["cancel-in-progress"])
    assert "github.event.action != 'edited'" in expr
    assert "github.event_name == 'pull_request'" in expr
    triggers = data.get("on", data.get(True))
    assert "edited" in triggers["pull_request"]["types"]
    # Same group for every action, so an edited run queues behind a synchronize
    # run instead of running beside it.
    assert "github.event.action" not in str(data["concurrency"]["group"])


@pytest.mark.unit
@pytest.mark.parametrize("name", POLLERS)
def test_poller_cancelled_is_pending_only_with_a_newer_run(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert re.search(r"failure\|cancelled\|", text) is None
    assert re.search(r"^\s+cancelled\)\n", text, re.M)
    assert "actions/workflows/call-occ-preflight.yml/runs?head_sha=" in text
    # Fail closed: a read error or a zero count refuses.
    assert "|| in_flight=0" in text
    cancelled_block = text.split("cancelled)\n", 1)[1].split(";;", 1)[0]
    assert "exit 1" in cancelled_block
    gh_line = next(ln for ln in cancelled_block.splitlines() if "gh api" in ln)
    assert "2>/dev/null" not in gh_line
    jobs = yaml.safe_load(text)["jobs"]
    poller = next(
        j for j in jobs.values() if j.get("name") == "OCC Preflight Dependency"
    )
    assert poller["permissions"]["actions"] == "read"
