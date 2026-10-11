# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20073 AC3, ruleset half: no queued PR is admitted around its hold.

The repo-evidence context is produced from ``pull_request_target`` and not for
``merge_group``, so a merge queue on ``dev`` would wait on a context nothing
reports for the queue. The queued-PR clause therefore holds because no queue is
active, and this test turns red the day one is enabled before queue evidence
support exists. The held-PR clause is the arming gate, bound separately to
``test_auto_merge_hold_omn18179.py``.

The rules are read from GitHub through the same ``gh`` reader the verifier uses
for required contexts. They only read; nothing here writes protection. The read
is public data on a public repository, and a read that fails fails the test; it
never skips.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    _gh_json,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
BRANCH = "dev"
EVIDENCE_CONTEXT = "repo-evidence / dod-verify"
GITHUB_ACTIONS_APP_ID = 15368
GH_TIMEOUT_S = 60
SNAPSHOT_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "omn20073_ruleset_readback.json"
)
# What `gh` says when it holds no credential (an Actions job without GH_TOKEN, a
# host never logged in, or no `gh` on PATH).
UNAUTHENTICATED_MARKERS = ("GH_TOKEN", "gh auth login", "No such file")

REPO_ROOT = Path(__file__).resolve().parents[2]
CALLER_PATH = REPO_ROOT / ".github" / "workflows" / "call-repo-evidence-gate.yml"


def _read(path: str) -> Any:
    """The GitHub payload at ``path``: live when ``gh`` is authenticated, else recorded.

    The evidence gate runs with the job's token and so reads live. The general
    unit-test job has no token, and a read that cannot authenticate would turn
    every pull request red, so there the same evaluators run over the payloads
    recorded in ``SNAPSHOT_PATH``. Any other failure of a live read fails.
    """
    data, detail = _gh_json(["gh", "api", path], GH_TIMEOUT_S)
    if data is not None:
        return data
    if not any(marker in detail for marker in UNAUTHENTICATED_MARKERS):
        pytest.fail(f"GitHub read of {path} failed: {detail}")
    recorded = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if path not in recorded:
        pytest.fail(f"no recorded payload for {path} in {SNAPSHOT_PATH.name}")
    return recorded[path]


def _dev_rules() -> list[dict[str, Any]]:
    rules = _read(f"repos/{REPO}/rules/branches/{BRANCH}")
    assert isinstance(rules, list)
    return rules


def evidence_source_violations(rules: list[dict[str, Any]]) -> list[str]:
    """The ruleset requires the repo-evidence context from GitHub Actions only."""
    entries = [
        check
        for rule in rules
        if rule.get("type") == "required_status_checks"
        for check in rule.get("parameters", {}).get("required_status_checks", [])
        if check.get("context") == EVIDENCE_CONTEXT
    ]
    if not entries:
        return [f"no active rule on {BRANCH} requires {EVIDENCE_CONTEXT!r}"]
    return [
        f"{EVIDENCE_CONTEXT!r} is required with integration_id "
        f"{entry.get('integration_id')!r}, not GitHub Actions "
        f"({GITHUB_ACTIONS_APP_ID})"
        for entry in entries
        if entry.get("integration_id") != GITHUB_ACTIONS_APP_ID
    ]


def merge_queue_violations(rules: list[dict[str, Any]]) -> list[str]:
    """A merge queue is active, so a PR could be queued."""
    return [
        "a merge_queue rule is active on dev"
        for rule in rules
        if rule.get("type") == "merge_queue"
    ]


def test_queue_not_admitted_without_a_queue_or_merge_group_evidence() -> None:
    rules = _dev_rules()
    assert merge_queue_violations(rules) == [], (
        "a merge queue is active on dev while repo-evidence / dod-verify is not "
        "produced for merge_group: queue evidence support (OR.2) must land first"
    )
    assert evidence_source_violations(rules) == []
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    # PyYAML 1.1 resolves the bare `on:` key to the boolean True.
    triggers = caller.get("on", caller.get(True))
    assert "merge_group" not in triggers


def test_queue_not_admitted_planted_payload_violates() -> None:
    assert merge_queue_violations([{"type": "merge_queue", "parameters": {}}])
    assert merge_queue_violations([{"type": "required_status_checks"}]) == []
    assert evidence_source_violations([])
