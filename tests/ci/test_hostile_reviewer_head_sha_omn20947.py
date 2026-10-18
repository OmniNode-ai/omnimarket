# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Hostile Reviewer passes the pull request's head commit to cli_review (OMN-20947).

A required voter that times out or is unavailable (a 502/503/504, a refused
connection, a failed reachability probe or served-model read) can be replaced
by clean signed off-CI review receipts, but only for the exact commit they
were issued against and only when the voters overlay declares
``substitute_votes``. cli_review reads that commit from ``--head-sha``; without
it no receipt is read and the voter stays lost. The head is the pull request's
head commit, never the merge ref the job checks out.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit]

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "hostile-reviewer.yml"
)
HEAD_SHA_EXPR = "${{ github.event.pull_request.head.sha }}"
HEAD_SHA_ARG = '--head-sha "$HEAD_SHA"'
# OMN-20910: the merged Claude API voter revision descends from the existing
# receipt-substitution revision and retains the exact-head receipt boundary.
SUBSTITUTE_PIN = "7f686d4bbca251dd3db49c1222ff8d6f738abd6c"


def review_step(workflow_text: str) -> dict[str, Any]:
    """The step that runs cli_review, found by its id."""
    parsed = yaml.safe_load(workflow_text)
    for job in parsed["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("id") == "review":
                return dict(step)
    raise AssertionError("no step with id 'review'")


def head_sha_problems(workflow_text: str) -> list[str]:
    """Why the review step does not hand cli_review the pull request head."""
    step = review_step(workflow_text)
    problems: list[str] = []
    if (step.get("env") or {}).get("HEAD_SHA") != HEAD_SHA_EXPR:
        problems.append("HEAD_SHA is not the pull request head sha")
    run = str(step.get("run", ""))
    if "omniintelligence.review_pairing.cli_review" not in run:
        problems.append("the review step does not run cli_review")
    if HEAD_SHA_ARG not in run:
        problems.append("cli_review is not passed --head-sha")
    return problems


def test_review_passes_the_pull_request_head_sha() -> None:
    assert head_sha_problems(WORKFLOW.read_text(encoding="utf-8")) == []


def test_a_review_step_without_the_head_sha_is_caught() -> None:
    """Positive control: the review step as it was before this change."""
    before = re.sub(
        r"^\s+HEAD_SHA: .*\n", "", WORKFLOW.read_text(encoding="utf-8"), flags=re.M
    ).replace(f"            {HEAD_SHA_ARG} \\\n", "")
    assert head_sha_problems(before) == [
        "HEAD_SHA is not the pull request head sha",
        "cli_review is not passed --head-sha",
    ]


def test_omniintelligence_is_cloned_at_the_substitute_revision() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    pins = re.findall(
        r"clone_with_retry omniintelligence \S+ OMNIINTELLIGENCE_SHA ([0-9a-f]{40})",
        text,
    )
    assert pins == [SUBSTITUTE_PIN]
