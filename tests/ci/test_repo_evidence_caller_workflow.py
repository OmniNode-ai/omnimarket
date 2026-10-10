# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20070: repo-owned acceptance evidence and its base-branch caller."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CALLER_PATH = REPO_ROOT / ".github" / "workflows" / "call-repo-evidence-gate.yml"


def test_caller_workflow_shape() -> None:
    text = CALLER_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    # PyYAML 1.1 resolves the bare `on:` key to the boolean True.
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict), "caller must declare a mapping on: block"
    assert "pull_request_target" in triggers, "caller must run from the base branch"
    assert "pull_request" not in triggers, "caller must not use pull_request"
    assert "workflow_run" not in triggers, "caller must not use workflow_run"
    target = triggers["pull_request_target"]
    assert target["branches"] == ["dev", "main"], "caller must target dev and main"
    assert target["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "edited",
        "ready_for_review",
    ], "caller must cover the declared PR activity types"
    assert data["permissions"] == {"contents": "read", "pull-requests": "read"}, (
        "caller permissions must be exactly contents: read and pull-requests: read"
    )
    assert set(data["jobs"]) == {"repo-evidence"}, (
        "caller must have one repo-evidence job"
    )
    job = data["jobs"]["repo-evidence"]
    assert re.fullmatch(
        r"OmniNode-ai/omnibase_core/\.github/workflows/receipt-gate\.yml@[0-9a-f]{40}",
        job["uses"],
    ), "receipt-gate reusable must be pinned by an immutable full SHA"
    assert job["with"]["evidence-source"] == "caller", (
        "caller evidence mode is required"
    )
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", job["with"]["verifier-version"]), (
        "verifier-version must be a numeric semantic version"
    )
    for key in ("steps", "secrets", "if", "name", "permissions"):
        assert key not in job, f"caller job must not declare {key}"
    assert "secrets: inherit" not in text, "caller must not inherit secrets"


def test_every_repo_contract_binds_every_criterion() -> None:
    assert CALLER_PATH.is_file(), "repo-owned evidence requires the caller workflow"
    contracts = sorted((REPO_ROOT / "contracts").glob("OMN-*.yaml"))
    assert contracts, "expected at least one repo-owned contracts/OMN-*.yaml"
    for path in contracts:
        contract = yaml.safe_load(path.read_text(encoding="utf-8"))
        criteria = {
            ac["id"]
            for requirement in contract.get("requirements", [])
            for ac in requirement.get("acceptance", [])
        }
        bound: set[str] = set()
        for item in contract.get("dod_evidence", []):
            label = f"{path.name}:{item['id']}"
            # The verifier's must-fail control only works on merged PRs; such
            # an item would be reported unavailable on an open PR.
            assert not item["id"].startswith("dod-occ-diff-derived-behavior-proof"), (
                f"{label}: merged-PR must-fail controls are unavailable on open PRs"
            )
            assert "ac_bindings" not in item, f"{label}: use binds_ac, not ac_bindings"
            if "binds_ac" not in item:
                continue
            bound.update(item["binds_ac"])
            checks = [
                check
                for check in item.get("checks", [])
                if check.get("check_type") == "test_passes"
                and check.get("check_value", "").startswith("uv run pytest ")
            ]
            selectors = [
                next(
                    (
                        token
                        for token in shlex.split(check["check_value"])
                        if token.endswith(".py") or token.endswith("/tests")
                    ),
                    "",
                )
                for check in checks
            ]
            # A criterion about a merged PR body is bound to a grep over that
            # body, which exits non-zero when the sentence is absent.
            body_checks = [
                check
                for check in item.get("checks", [])
                if check.get("check_type") == "command"
                and check.get("check_value", "").startswith("gh pr view ")
                and "--json body" in check["check_value"]
                and "| grep -Eq " in check["check_value"]
            ]
            if body_checks:
                continue
            assert any(
                selector
                and not Path(selector).is_absolute()
                and (REPO_ROOT / selector).resolve().is_relative_to(REPO_ROOT)
                and (REPO_ROOT / selector).exists()
                for selector in selectors
            ), (
                f"{label}: binds_ac requires uv run pytest evidence naming an existing repo test file"
            )
        assert criteria <= bound, (
            f"{path.name}: acceptance criteria missing binds_ac: {sorted(criteria - bound)}"
        )


# OMN-20073: omnibase_core#1884 added the dependency-bot and pin-only writer-app
# exemption to the caller-evidence dod-verify job. A pin that predates it makes
# every bot version bump fail for binding no evidence of its own.
# OMN-20543: omnibase_core#1886, a descendant of #1884, gives that job a
# Postgres service, the PG16 server tools and INTEGRATION_POSTGRES_*, so a bound
# test that needs a database runs at the head and in the merge-base control.
# The pin moved to a descendant (omnibase_core#1912, OMN-17427; then omnibase_core#1914, OMN-20074).
_EXEMPT_REUSABLE_SHA = "fb0c6c2117d5868a398b0920cd0048d0824415b1"


def test_caller_pins_a_reusable_with_the_bot_bump_exemption() -> None:
    uses = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))["jobs"][
        "repo-evidence"
    ]["uses"]
    assert uses.endswith(f"@{_EXEMPT_REUSABLE_SHA}"), (
        "pin the omnibase_core reusable at or past omnibase_core#1884"
    )


# OMN-20073 (S6 cut-over, AC5): the S5 pilot met its bar and dev branch
# protection no longer requires any OCC context, so the caller turns the
# reusable's difference step off. With it on, the step waits for an
# occ-preflight / eligibility verdict on the head and fails closed without one,
# so the PR that deletes the OCC callers could never pass. The pinned verifier
# stays a release that ships the classifier (omnimarket#3277, 0.4.294), so the
# step can be turned back on by this one input if the cut-over is rolled back.
_DIFFERENCE_CLASSIFIER_FLOOR = (0, 4, 294)


def test_caller_stops_comparing_with_occ_after_the_s6_cutover() -> None:
    job = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))["jobs"][
        "repo-evidence"
    ]
    assert job["with"].get("compare-with-occ") == "false", (
        'after the S6 cut-over the caller passes compare-with-occ: "false" '
        "(a quoted string input): there is no OCC verdict left to compare"
    )
    version = tuple(int(part) for part in job["with"]["verifier-version"].split("."))
    assert version >= _DIFFERENCE_CLASSIFIER_FLOOR, (
        "verifier-version must ship node_dod_verify occ-difference "
        f"(>= {'.'.join(map(str, _DIFFERENCE_CLASSIFIER_FLOOR))})"
    )


# OMN-20070 (AC3): omnimarket#3511 (e565ba5ba) makes the verifier refuse a
# contract that leaves one acceptance criterion unbound, first released in
# 0.4.303. A caller pinned before that release admits such a contract, so the
# required repo-evidence / dod-verify check would pass a half-bound contract.
# 0.4.305 is the release omnibase_infra and omnibase_core run: it ships that
# refusal, the occ-difference classifier and the release-cut classifiers.
_UNBOUND_REFUSAL_FLOOR = (0, 4, 305)


def test_caller_pins_a_verifier_that_refuses_unbound_criteria() -> None:
    job = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))["jobs"][
        "repo-evidence"
    ]
    version = tuple(int(part) for part in job["with"]["verifier-version"].split("."))
    assert version >= _UNBOUND_REFUSAL_FLOOR, (
        "verifier-version must ship the unbound-criteria refusal "
        f"(omnimarket#3511, >= {'.'.join(map(str, _UNBOUND_REFUSAL_FLOOR))})"
    )


# OMN-20543 (AC3): the pinned reusable is the one whose dod-verify job gives a
# database test its database. A pin before omnibase_core#1886 runs that test
# with none, so a contract can only scope it local_done_gate, which the hosted
# control refuses as a control that did not run (omnimarket#3369).
# The pin moved to a descendant (omnibase_core#1912, OMN-17427; then omnibase_core#1914, OMN-20074).
_DATABASE_REUSABLE_SHA = "fb0c6c2117d5868a398b0920cd0048d0824415b1"


def test_caller_pins_a_reusable_that_gives_bound_tests_a_database() -> None:
    uses = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))["jobs"][
        "repo-evidence"
    ]["uses"]
    assert uses.endswith(f"@{_DATABASE_REUSABLE_SHA}"), (
        "pin the omnibase_core reusable at or past omnibase_core#1886"
    )
