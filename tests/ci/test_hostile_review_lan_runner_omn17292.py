# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review job needs labels unavailable on off-LAN h203-light runners.

Ports the LAN fallback and off-LAN exclusion checks from omnibase_infra's
test_hostile_review_runner_and_degraded_omn17492.py, using this repo's seam.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github/workflows/hostile-reviewer.yml"
)
DEDICATED_VARIABLE = "vars.OMNI_REVIEW_RUNS_ON_JSON"
LAN_FLEET_FALLBACK = """'["self-hosted","omnibase-ci","arch-amd64"]'"""


def _runs_on() -> str:
    return str(
        yaml.safe_load(WORKFLOW.read_text())["jobs"]["hostile-review"]["runs-on"]
    )


def _fallback_labels() -> set[str]:
    match = re.search(r"vars\.OMNI_REVIEW_RUNS_ON_JSON\s*\|\|\s*'([^']+)'", _runs_on())
    assert match is not None, "the review job must retain its dedicated runner seam"
    return {label.lower() for label in json.loads(match.group(1))}


def test_trusted_branch_reads_the_dedicated_variable_with_the_lan_fallback() -> None:
    assert f"{DEDICATED_VARIABLE} || {LAN_FLEET_FALLBACK}" in _runs_on()


def test_lan_fallback_requires_a_label_the_off_lan_runners_lack() -> None:
    fallback_labels = _fallback_labels()
    h203_labels = {"self-hosted", "omnibase-ci", "h203-light", "linux", "x64"}
    assert not h203_labels.issuperset(fallback_labels), (
        "a fallback satisfied by h203-light can route review to unreachable voters"
    )
    lan_labels = {"self-hosted", "omnibase-ci", "arch-amd64", "linux", "x64"}
    assert lan_labels.issuperset(fallback_labels)


def test_fork_pull_requests_still_take_the_public_path_first() -> None:
    runs_on = _runs_on()
    assert "github.event_name == 'pull_request'" in runs_on
    assert (
        "github.event.pull_request.head.repo.full_name != github.repository" in runs_on
    )
    assert (
        "fromJSON(vars.OMNI_PUBLIC_PR_RUNS_ON_JSON || '[\"ubuntu-latest\"]')" in runs_on
    )
    assert runs_on.index("OMNI_PUBLIC_PR_RUNS_ON_JSON") < runs_on.index(
        DEDICATED_VARIABLE
    )
