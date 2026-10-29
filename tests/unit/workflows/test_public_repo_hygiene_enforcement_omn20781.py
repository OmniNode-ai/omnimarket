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
    assert re.fullmatch(r"dev-\d{14}-[0-9a-f]{12}", pin), pin
    assert pin != PRE_ADDED_LINES_PIN


def _assert_staged_paths_only(hook: dict[str, Any]) -> None:
    """OMN-17427: the only narrowing a hygiene hook may carry is the staged paths.

    The exported hook scans the whole tracked tree on every commit (~15 to 35 s).
    Under ``enforce_scope: added-lines`` the verdict is the findings on the lines
    the index adds, and every such line sits in a staged file, so the commit is
    handed the staged paths (``--staged``). Any other override would let a
    literal through, and a whole-tree config would make ``--staged`` narrow it.
    """
    assert hook.get("args") == ["--staged"]
    assert hook.get("pass_filenames") is True
    assert hook.get("always_run") is False
    assert hook.get("stages") == ["pre-commit"]
    for narrowing in ("files", "exclude", "types", "entry"):
        assert narrowing not in hook, narrowing
    config = yaml.safe_load((ROOT / ".public-repo-hygiene.yaml").read_text())
    assert config["enforce_scope"] == "added-lines"


def test_precommit_hook_runs_the_gate_at_the_workflow_pin() -> None:
    repo, hook = _precommit_hygiene_hook()
    assert repo["rev"].split()[0] == _workflow_pin()
    assert hook.get("require_serial") is True
    _assert_staged_paths_only(hook)


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


def test_hygiene_context_is_not_recorded_as_exempt() -> None:
    from tests.unit.scripts.ci.test_ci_summary_gate import EXEMPT_CONTEXTS

    assert ("public-repo-hygiene.yml", "public-repo-hygiene") not in EXEMPT_CONTEXTS


DEPLOYMENT_CONTEXT = "public-repo-hygiene / deployment-config"


def test_precommit_runs_the_deployment_config_hook_at_the_workflow_pin() -> None:
    """OMN-20940: the local twin of the required deployment-config context."""
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    matches = [
        (repo, hook)
        for repo in config["repos"]
        if repo["repo"] == "https://github.com/OmniNode-ai/omniclaude"
        for hook in repo["hooks"]
        if hook["id"] == "deployment-config"
    ]
    assert len(matches) == 1, "exactly one deployment-config hook is declared"
    repo, hook = matches[0]
    assert repo["rev"].split()[0] == _workflow_pin()
    _assert_staged_paths_only(hook)


def test_deployment_config_context_is_asserted_by_the_ci_summary() -> None:
    assert DEPLOYMENT_CONTEXT in EXPECTED_EXTERNAL_CONTEXTS
    producer = EXTERNAL_CONTEXT_PRODUCERS[DEPLOYMENT_CONTEXT]
    assert producer.workflow == "public-repo-hygiene.yml"
    assert "pull_request" in producer.events


def test_queue_commit_reports_the_deployment_config_context() -> None:
    workflow = yaml.safe_load(
        (
            ROOT / ".github" / "workflows" / "merge-group-pr-scoped-contexts.yml"
        ).read_text()
    )
    names = {job.get("name") for job in workflow["jobs"].values()}
    assert DEPLOYMENT_CONTEXT in names
