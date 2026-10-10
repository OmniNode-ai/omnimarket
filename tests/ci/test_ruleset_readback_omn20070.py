# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20070 AC5: the scratch-PR outcomes and the ruleset source, read live.

The repo-evidence gate is only as strong as the ruleset that requires it, and a
ruleset is not a file in this repository. These tests read what GitHub holds
for ``OmniNode-ai/omnimarket`` branch ``dev`` (the rules, the branch
protection and the recorded check runs) through the same ``gh`` reader the
verifier uses for required contexts, and assert exactly what each criterion
claims. They only read: nothing here writes protection.

Every read is public data on a public repository, so the hosted verifier makes
it live with the job's own token. Where ``gh`` holds no credential (the general
unit-test job), the same evaluators run over the payloads recorded in
``fixtures/omn20070_ruleset_readback.json``; any other failed read fails the
test, and nothing skips.

Each pure evaluator below is also fed a planted payload that must violate it, so
a green live read is never the only evidence that the check can fail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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
    Path(__file__).resolve().parent / "fixtures" / "omn20070_ruleset_readback.json"
)
# What `gh` says when it holds no credential (an Actions job without GH_TOKEN, a
# host never logged in, or no `gh` on PATH).
UNAUTHENTICATED_MARKERS = ("GH_TOKEN", "gh auth login", "No such file")
PILOT_EVIDENCE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "omn20072_s5_pilot_evidence.json"
)

# Recorded S5 observations (OMN-20072 pilot record, read back from GitHub).
REAL_FIX_CHECK_RUN = 110981993271  # scratch omnimarket#3257, bound test merges green
ALWAYS_PASS_CHECK_RUNS = (
    110982013438,  # scratch omnimarket#3258, bound test also passes at the base
    111536237889,  # omnimarket#3388, case C18
)
WORKFLOW_EDIT_SUITE = (
    100822311744  # omnimarket#3381: caller edited to evidence-source occ
)
TOKEN_STATUS_RULE_SUITE = 4404187383  # case C16, refused by ruleset 24462257


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


def _check_run(check_run_id: int) -> dict[str, Any]:
    run = _read(f"repos/{REPO}/check-runs/{check_run_id}")
    assert isinstance(run, dict)
    return run


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


# ---------------------------------------------------------------------------
# OMN-20070 AC5: the four scratch-PR outcomes, read back from the recorded
# check runs, each by the required context's own run and app.
# ---------------------------------------------------------------------------


def _is_required_context_run(run: dict[str, Any]) -> bool:
    return (
        run.get("name") == EVIDENCE_CONTEXT
        and run.get("app", {}).get("id") == GITHUB_ACTIONS_APP_ID
        and run.get("status") == "completed"
    )


def test_scratch_outcomes_real_fix_is_green() -> None:
    run = _check_run(REAL_FIX_CHECK_RUN)
    assert _is_required_context_run(run)
    assert run["conclusion"] == "success"


@pytest.mark.parametrize("check_run_id", ALWAYS_PASS_CHECK_RUNS)
def test_scratch_outcomes_always_pass_is_refused_by_the_required_context(
    check_run_id: int,
) -> None:
    run = _check_run(check_run_id)
    assert _is_required_context_run(run)
    assert run["conclusion"] == "failure"


def test_scratch_outcomes_workflow_edit_does_not_change_the_definition() -> None:
    """The edited caller said evidence-source occ, which skips dod-verify.

    omnimarket#3381 edited ``evidence-source: caller`` to ``occ`` in the caller.
    Had the edited definition run, ``repo-evidence / dod-verify`` would be
    skipped and ``repo-evidence / verify`` would run. The base branch's
    definition ran: dod-verify executed and verify is the skipped one.
    """
    suite = _read(f"repos/{REPO}/check-suites/{WORKFLOW_EDIT_SUITE}/check-runs")
    assert isinstance(suite, dict)
    by_name = {run["name"]: run for run in suite["check_runs"]}
    assert by_name[EVIDENCE_CONTEXT]["conclusion"] != "skipped"
    assert by_name["repo-evidence / verify"]["conclusion"] == "skipped"
    details_url = by_name[EVIDENCE_CONTEXT]["details_url"]
    workflow_run_id = int(details_url.split("/runs/")[1].split("/")[0])
    workflow_run = _read(f"repos/{REPO}/actions/runs/{workflow_run_id}")
    assert workflow_run["event"] == "pull_request_target"
    assert workflow_run["path"] == ".github/workflows/call-repo-evidence-gate.yml"


def test_scratch_outcomes_token_status_is_refused_by_the_ruleset_source() -> None:
    """A status of the same name from a token is not accepted.

    The ruleset names GitHub Actions as the only source for the context, which
    is why GitHub reports a user-token status as "not set by the expected
    GitHub app". The refusal itself is recorded as case C16 (rule suite
    4404187383); the rule suite endpoint needs repository admin, so the
    recorded observation is read from the pilot evidence and the mechanism
    from the live ruleset.
    """
    assert evidence_source_violations(_dev_rules()) == []
    cases = json.loads(PILOT_EVIDENCE_PATH.read_text(encoding="utf-8"))["cases"]["rows"]
    c16 = next(row for row in cases if row["case"] == "C16")
    assert str(TOKEN_STATUS_RULE_SUITE) in c16["note"]
    assert "422" in c16["note"]


def test_scratch_outcomes_planted_runs_violate() -> None:
    assert not _is_required_context_run(
        {
            "name": "CI Summary",
            "app": {"id": GITHUB_ACTIONS_APP_ID},
            "status": "completed",
        }
    )
    assert not _is_required_context_run(
        {"name": EVIDENCE_CONTEXT, "app": {"id": 1}, "status": "completed"}
    )
    assert not _is_required_context_run(
        {
            "name": EVIDENCE_CONTEXT,
            "app": {"id": GITHUB_ACTIONS_APP_ID},
            "status": "queued",
        }
    )
