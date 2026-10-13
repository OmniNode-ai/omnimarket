# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The remaining checks of the script, its JSONL records, counts and exit status.

The positive control comes first: a dev branch missing a required context must
produce a FAIL finding naming the check and a non-zero result. Every zero
elsewhere in this suite is meaningful only beside it.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    EnumBranchProtectionCheckStatus,
    ModelBranchProtectionFinding,
)
from tests.nodes.node_branch_protection_audit_effect.audit_fixtures import (
    GQL_RULES,
    ORDINARY_REPO,
    ORG,
    POLICY,
    PROTECTION_CI_SUMMARY,
    REPO_SETTINGS,
    RULESET_MERGE_QUEUE_DISABLED,
    run_audit,
)

pytestmark = pytest.mark.unit

FAIL = EnumBranchProtectionCheckStatus.FAIL
PASS = EnumBranchProtectionCheckStatus.PASS

# A public repository that is in no list: dev is asserted with the ordinary checks.
PLAIN_REPO = "plain_repo"

_PROTECTION_VERIFY_ONLY: dict[str, object] = {
    "required_status_checks": {
        "strict": False,
        "contexts": ["verify / verify"],
        "checks": [{"context": "verify / verify", "app_id": 15368}],
    },
    "enforce_admins": {"enabled": True},
}


def _fixtures(
    repo: str,
    dev: dict[str, object] | None = PROTECTION_CI_SUMMARY,
    main: dict[str, object] | None = PROTECTION_CI_SUMMARY,
    settings: dict[str, object] | None = REPO_SETTINGS,
    rulesets: list[dict[str, object]] | None = None,
    gql: dict[str, object] | None = GQL_RULES,
) -> dict[str, object]:
    base = f"repos/{ORG}/{repo}"
    fx: dict[str, object] = {}
    if gql is not None:
        fx["graphql"] = gql
    if dev is not None:
        fx[f"{base}/branches/dev/protection"] = dev
    if main is not None:
        fx[f"{base}/branches/main/protection"] = main
    if settings is not None:
        fx[base] = settings
    listed = [RULESET_MERGE_QUEUE_DISABLED] if rulesets is None else rulesets
    fx[f"{base}/rulesets"] = listed
    for rs in listed:
        fx[f"{base}/rulesets/{rs['id']}"] = rs
    return fx


def _finding(
    repo: str, branch: str, check: str, status: EnumBranchProtectionCheckStatus
) -> ModelBranchProtectionFinding | None:
    result = run_audit(repo, _fixtures(repo), branches="dev")
    for f in result.findings:
        if (f.repo, f.branch, f.check, f.status) == (repo, branch, check, status):
            return f
    return None


def test_positive_control_missing_required_context_is_a_finding_and_nonzero() -> None:
    result = run_audit(
        PLAIN_REPO, _fixtures(PLAIN_REPO, dev=_PROTECTION_VERIFY_ONLY), branches="dev"
    )
    assert result.exit_code == 1, result.report
    assert result.failures == 1
    assert result.noncompliant_repos == (PLAIN_REPO,)
    assert (
        ModelBranchProtectionFinding(
            repo=PLAIN_REPO,
            branch="dev",
            check="required_check_ci_summary",
            status=FAIL,
            detail="CI Summary missing",
        )
        in result.findings
    )
    assert '[dev] FAIL: "CI Summary" not found in required status checks' in (
        result.report
    )
    assert "  >>> NON-COMPLIANT" in result.report


def test_compliant_dev_counts_every_check_and_exits_zero() -> None:
    result = run_audit(PLAIN_REPO, _fixtures(PLAIN_REPO), branches="dev")
    assert result.exit_code == 0, result.report
    assert result.failures == 0
    # reviews, CI Summary, enforce_admins, app binding, delete_branch_on_merge,
    # Merge Queue ruleset, merge-queue bypass.
    assert result.total_checks == 7
    assert " Summary: 0 failure(s) across 7 checks" in result.report
    assert result.report.rstrip().endswith("All repos compliant.")
    assert [(f.check, f.status) for f in result.findings] == [
        ("reviews_not_enforced", PASS),
        ("required_check_ci_summary", PASS),
        ("enforce_admins", PASS),
        ("required_check_app_binding", PASS),
        ("delete_branch_on_merge", PASS),
        ("merge_queue_ruleset", PASS),
        ("merge_queue_ruleset_no_bypass", PASS),
    ]


def test_unfetchable_branch_protection_fails_without_counting_a_check() -> None:
    result = run_audit(PLAIN_REPO, _fixtures(PLAIN_REPO, dev=None), branches="dev")
    assert result.exit_code == 1
    assert "[dev] FAIL: Could not fetch branch protection (is it enabled?)" in (
        result.report
    )
    assert ("branch_protection_fetch", FAIL) in [
        (f.check, f.status) for f in result.findings
    ]
    # delete_branch_on_merge, Merge Queue ruleset, merge-queue bypass.
    assert result.total_checks == 3


def test_enforce_admins_disabled_fails() -> None:
    dev = {**PROTECTION_CI_SUMMARY, "enforce_admins": {"enabled": False}}
    result = run_audit(PLAIN_REPO, _fixtures(PLAIN_REPO, dev=dev), branches="dev")
    assert result.exit_code == 1
    assert "[dev] FAIL: enforce_admins is not enabled" in result.report


def test_unreadable_review_rules_warn_and_do_not_fail() -> None:
    result = run_audit(PLAIN_REPO, _fixtures(PLAIN_REPO, gql=None), branches="dev")
    assert result.exit_code == 0, result.report
    assert (
        "[dev] WARN: could not determine review enforcement via GraphQL "
        "(no matching rule)"
    ) in result.report


def test_delete_branch_on_merge_false_fails() -> None:
    settings = {"delete_branch_on_merge": False}
    result = run_audit(
        PLAIN_REPO, _fixtures(PLAIN_REPO, settings=settings), branches="dev"
    )
    assert result.exit_code == 1
    assert "  [repo] FAIL: delete_branch_on_merge is not true" in result.report


def test_unfetchable_repo_settings_fail() -> None:
    result = run_audit(PLAIN_REPO, _fixtures(PLAIN_REPO, settings=None), branches="dev")
    assert result.exit_code == 1
    assert "  [repo] FAIL: Could not fetch repo settings" in result.report


def test_missing_merge_queue_ruleset_fails() -> None:
    result = run_audit(PLAIN_REPO, _fixtures(PLAIN_REPO, rulesets=[]), branches="dev")
    assert result.exit_code == 1
    assert '  [repo] FAIL: "Merge Queue" ruleset not found' in result.report


def test_private_repo_skips_the_merge_queue_check() -> None:
    result = run_audit(
        ORDINARY_REPO, _fixtures(ORDINARY_REPO, rulesets=[]), branches="dev"
    )
    assert result.exit_code == 0, result.report
    assert "  [repo] SKIP: Merge Queue ruleset check (private repo)" in result.report


def test_dev_exempt_repo_skips_dev() -> None:
    policy = POLICY.model_copy(update={"dev_exempt_repos": (PLAIN_REPO,)})
    result = run_audit(
        PLAIN_REPO, _fixtures(PLAIN_REPO, dev=None), branches="dev", policy=policy
    )
    assert result.exit_code == 0, result.report
    assert "    [dev] SKIP: repo has no protected dev branch (dev-exempt)" in (
        result.report
    )


def test_receipt_gate_is_asserted_on_main_and_noted_on_dev() -> None:
    repo = ORDINARY_REPO
    main = {
        "required_status_checks": {
            "strict": False,
            "contexts": ["CI Summary"],
            "checks": [{"context": "CI Summary", "app_id": 15368}],
        },
        "enforce_admins": {"enabled": True},
    }
    failing = run_audit(repo, _fixtures(repo, main=main), branches="main")
    assert failing.exit_code == 1
    assert '[main] FAIL: "verify / verify" Receipt Gate not found' in failing.report

    noted = run_audit(repo, _fixtures(repo, dev=main), branches="dev")
    assert noted.exit_code == 0, noted.report
    assert '[dev] NOTE: "verify / verify" Receipt Gate absent on dev' in noted.report


def test_report_frames_the_repo_and_the_header_lists_the_policy() -> None:
    result = run_audit(PLAIN_REPO, _fixtures(PLAIN_REPO), branches="dev")
    lines = result.report.splitlines()
    assert " Branch Protection Audit" in lines
    assert f" Org: {ORG}  |  Branches: dev" in lines
    assert f"Repo: {ORG}/{PLAIN_REPO}" in lines
    assert "  ── branch: dev ───────────────────" in lines
    assert "  >>> COMPLIANT" in lines
