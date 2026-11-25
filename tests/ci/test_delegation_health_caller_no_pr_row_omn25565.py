# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A pull request head carries no delegation-health row (OMN-25565).

The caller reads verdicts from lab, stability and customer-surface runs, so a
red verdict elsewhere put a red row on every pull request head. The caller
runs on a schedule and on push to dev only.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "delegation-health-check.yml"
)
PR_TRIGGERS = {"pull_request", "pull_request_target", "merge_group"}


def _triggers(text: str) -> set[str]:
    doc = yaml.safe_load(text)
    # PyYAML reads the bare key `on` as the boolean True.
    on = doc.get("on", doc.get(True))
    return set(on) if isinstance(on, dict) else {on} if isinstance(on, str) else set(on)


def test_the_caller_runs_on_no_pull_request_or_merge_group() -> None:
    assert not (_triggers(WORKFLOW.read_text(encoding="utf-8")) & PR_TRIGGERS)


def test_the_caller_still_runs_on_schedule_and_push() -> None:
    assert {"schedule", "push"} <= _triggers(WORKFLOW.read_text(encoding="utf-8"))


def test_a_pull_request_trigger_would_be_found() -> None:
    """Positive control: the scan names a caller that runs on pull_request."""
    text = "name: x\non:\n  pull_request:\n  push:\n    branches: [dev]\njobs: {}\n"
    assert _triggers(text) & PR_TRIGGERS == {"pull_request"}
