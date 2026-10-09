# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The public-repo hygiene gate blocks on lines a pull request adds (OMN-20781).

omniclaude's gate fails a run for the five internal-content classes on added
lines only when the caller repository declares `enforce_scope: added-lines` and
`enforce_classes`, and when the caller's pin is at or after the commit that
taught the gate that scope. These tests read this repository's own
declarations, so they run on every runner with no omniclaude checkout.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.ci_summary_gate import (
    EXPECTED_EXTERNAL_CONTEXTS,
    EXTERNAL_CONTEXT_PRODUCERS,
)

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit

FIVE_CLASSES = {
    "private-repo-name",
    "internal-kb-prose",
    "lab-config",
    "person-name",
    "private-network",
}
# The pin this repository carried before added-lines support. The gate at this
# commit ignores `enforce_scope`, so a caller still pinned here blocks nothing.
PRE_ADDED_LINES_PIN = "6ccd525e2b6390869d18f8e00d1b36294aa5550a"
OMNICLAUDE_REUSABLE = (
    "OmniNode-ai/omniclaude/.github/workflows/public-repo-hygiene-reusable.yml@"
)
CONTEXT = "public-repo-hygiene / public-repo-hygiene"


def _hygiene_config() -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".public-repo-hygiene.yaml").read_text())


def _workflow_pin() -> str:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "public-repo-hygiene.yml").read_text()
    )
    uses = workflow["jobs"]["public-repo-hygiene"]["uses"]
    assert uses.startswith(OMNICLAUDE_REUSABLE), uses
    return uses.removeprefix(OMNICLAUDE_REUSABLE)


def _precommit_hygiene_hook() -> tuple[dict[str, Any], dict[str, Any]]:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    matches = [
        (repo, hook)
        for repo in config["repos"]
        if repo["repo"] == "https://github.com/OmniNode-ai/omniclaude"
        for hook in repo["hooks"]
        if hook["id"] == "public-repo-hygiene"
    ]
    assert len(matches) == 1, "exactly one public-repo-hygiene hook is declared"
    return matches[0]


def test_config_enforces_the_five_classes_on_added_lines() -> None:
    config = _hygiene_config()
    assert config["enforce_scope"] == "added-lines"
    assert set(config["enforce_classes"]) == FIVE_CLASSES
    assert len(config["enforce_classes"]) == len(FIVE_CLASSES)


def test_workflow_pin_is_past_added_lines_support() -> None:
    pin = _workflow_pin()
    assert re.fullmatch(r"[0-9a-f]{40}", pin), pin
    assert pin != PRE_ADDED_LINES_PIN


def test_precommit_hook_runs_the_gate_at_the_workflow_pin() -> None:
    repo, hook = _precommit_hygiene_hook()
    assert repo["rev"].split()[0] == _workflow_pin()
    assert hook.get("require_serial") is True
    # No narrowing: the manifest runs the gate always, over the staged added
    # lines, and any override of these fields would let a literal through.
    for narrowing in ("args", "files", "exclude", "types", "stages", "entry"):
        assert narrowing not in hook, narrowing


def test_hygiene_context_is_asserted_by_the_ci_summary() -> None:
    assert CONTEXT in EXPECTED_EXTERNAL_CONTEXTS
    producer = EXTERNAL_CONTEXT_PRODUCERS[CONTEXT]
    assert producer.workflow == "public-repo-hygiene.yml"
    assert "pull_request" in producer.events


def test_queue_commit_reports_the_hygiene_context() -> None:
    workflow = yaml.safe_load(
        (
            ROOT / ".github" / "workflows" / "merge-group-pr-scoped-contexts.yml"
        ).read_text()
    )
    names = {job.get("name") for job in workflow["jobs"].values()}
    assert CONTEXT in names
