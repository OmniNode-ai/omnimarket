# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Release-synced main, the ordinary main and the review-gated main.

Ported from onex_change_control ``tests/test_audit_branch_protection_release_synced.py``.

On a release-synced repository ``main`` is not a pull-request target: release
automation fast-forwards it to the published tag, and its required status
checks are empty on purpose, because a PR-shaped context can never report on
that ref. What protects it is an active branch ruleset over ``refs/heads/main``
carrying the ``update`` rule with at least one bypass actor. Both halves are
asserted and neither alone passes. Ordinary mains keep the "CI Summary"
assertion; the review-gated main keeps approving and code-owner reviews, and
its dev is ordinary solo-dev.
"""

from __future__ import annotations

import copy

import pytest

from tests.nodes.node_branch_protection_audit_effect.audit_fixtures import (
    GQL_REVIEW_GATED_MAIN,
    GQL_RULES,
    ORDINARY_REPO,
    ORG,
    PROTECTION_CI_SUMMARY,
    PROTECTION_NO_CONTEXTS,
    REPO_SETTINGS,
    RULESET_MERGE_QUEUE_DISABLED,
    RULESET_RESTRICTING_MAIN,
    review_rule,
    run_audit,
)

pytestmark = pytest.mark.unit

RELEASE_SYNCED_REPO = "omnibase_core"
REVIEW_GATED_REPO = "onex_change_control"


def _fixtures(
    repo: str,
    protection: dict[str, object],
    rulesets: list[dict[str, object]],
    gql_rules: dict[str, object] | None = None,
) -> dict[str, object]:
    base = f"repos/{ORG}/{repo}"
    fx: dict[str, object] = {
        "graphql": gql_rules if gql_rules is not None else GQL_RULES,
        f"{base}/branches/main/protection": protection,
        f"{base}/branches/dev/protection": PROTECTION_CI_SUMMARY,
        base: REPO_SETTINGS,
        f"{base}/rulesets": rulesets,
    }
    for rs in rulesets:
        fx[f"{base}/rulesets/{rs['id']}"] = rs
    return fx


def test_release_synced_main_passes_with_empty_contexts_and_ruleset() -> None:
    result = run_audit(
        RELEASE_SYNCED_REPO,
        _fixtures(
            RELEASE_SYNCED_REPO,
            PROTECTION_NO_CONTEXTS,
            [RULESET_MERGE_QUEUE_DISABLED, RULESET_RESTRICTING_MAIN],
        ),
    )
    assert result.exit_code == 0, result.report
    assert "required_status_checks is empty" in result.report
    assert "restricts updates to refs/heads/main" in result.report
    # The retired invariant must no longer be asserted on this main.
    assert '"CI Summary" not found' not in result.report


def test_release_synced_main_fails_without_ruleset() -> None:
    """Empty contexts with no push restriction is an unprotected main."""
    result = run_audit(
        RELEASE_SYNCED_REPO,
        _fixtures(
            RELEASE_SYNCED_REPO, PROTECTION_NO_CONTEXTS, [RULESET_MERGE_QUEUE_DISABLED]
        ),
    )
    assert result.exit_code == 1, result.report
    assert "no active ruleset restricts updates to refs/heads/main" in result.report


def test_release_synced_main_fails_when_ruleset_has_no_bypass_actor() -> None:
    """A restriction nobody can bypass freezes the release boundary."""
    frozen = dict(RULESET_RESTRICTING_MAIN)
    frozen["bypass_actors"] = []
    result = run_audit(
        RELEASE_SYNCED_REPO,
        _fixtures(
            RELEASE_SYNCED_REPO,
            PROTECTION_NO_CONTEXTS,
            [RULESET_MERGE_QUEUE_DISABLED, frozen],
        ),
    )
    assert result.exit_code == 1, result.report
    assert "no active ruleset restricts updates to refs/heads/main" in result.report


def test_release_synced_main_fails_when_contexts_are_reintroduced() -> None:
    """A required PR context on a fast-forward-only ref blocks the sync."""
    result = run_audit(
        RELEASE_SYNCED_REPO,
        _fixtures(
            RELEASE_SYNCED_REPO,
            PROTECTION_CI_SUMMARY,
            [RULESET_MERGE_QUEUE_DISABLED, RULESET_RESTRICTING_MAIN],
        ),
    )
    assert result.exit_code == 1, result.report
    assert "release-synced main carries" in result.report


def test_ordinary_repo_still_requires_ci_summary_on_main() -> None:
    """The compliant set keeps the original main assertion, unchanged."""
    passing = run_audit(
        ORDINARY_REPO,
        _fixtures(ORDINARY_REPO, PROTECTION_CI_SUMMARY, [RULESET_MERGE_QUEUE_DISABLED]),
    )
    assert passing.exit_code == 0, passing.report
    assert '"CI Summary" is a required status check' in passing.report

    failing = run_audit(
        ORDINARY_REPO,
        _fixtures(
            ORDINARY_REPO,
            PROTECTION_NO_CONTEXTS,
            [RULESET_MERGE_QUEUE_DISABLED, RULESET_RESTRICTING_MAIN],
        ),
    )
    assert failing.exit_code == 1, failing.report
    assert '"CI Summary" not found in required status checks' in failing.report


def test_review_gated_main_requires_approving_and_code_owner_reviews() -> None:
    """The review-gated main keeps ordinary checks plus review enforcement."""
    result = run_audit(
        REVIEW_GATED_REPO,
        _fixtures(
            REVIEW_GATED_REPO,
            PROTECTION_CI_SUMMARY,
            [RULESET_MERGE_QUEUE_DISABLED],
            gql_rules=GQL_REVIEW_GATED_MAIN,
        ),
    )
    assert result.exit_code == 0, result.report
    assert "approving and code-owner reviews are enforced" in result.report


def test_review_gated_main_fails_without_code_owner_reviews() -> None:
    """Approving reviews without the code-owner gate are insufficient."""
    gql_rules = copy.deepcopy(GQL_REVIEW_GATED_MAIN)
    review_rule(gql_rules, 0)["requiresCodeOwnerReviews"] = False
    result = run_audit(
        REVIEW_GATED_REPO,
        _fixtures(
            REVIEW_GATED_REPO,
            PROTECTION_CI_SUMMARY,
            [RULESET_MERGE_QUEUE_DISABLED],
            gql_rules=gql_rules,
        ),
    )
    assert result.exit_code == 1, result.report
    assert (
        "review-gated main requires approving and code-owner reviews" in result.report
    )


def test_review_gated_repo_dev_reviews_absent_passes() -> None:
    """The carve-out is main-only: dev is ordinary solo-dev and reviews are absent."""
    result = run_audit(
        REVIEW_GATED_REPO,
        _fixtures(
            REVIEW_GATED_REPO,
            PROTECTION_CI_SUMMARY,
            [RULESET_MERGE_QUEUE_DISABLED],
            gql_rules=GQL_REVIEW_GATED_MAIN,
        ),
        branches="dev",
    )
    assert result.exit_code == 0, result.report
    assert "approving reviews not enforced" in result.report


def test_review_gated_repo_dev_reviews_reenabled_fails() -> None:
    """Reviews re-enabled on the review-gated repo's dev must fail as solo-dev drift."""
    gql_rules = copy.deepcopy(GQL_REVIEW_GATED_MAIN)
    dev_rule = review_rule(gql_rules, 1)
    dev_rule["requiresApprovingReviews"] = True
    dev_rule["requiresCodeOwnerReviews"] = True
    result = run_audit(
        REVIEW_GATED_REPO,
        _fixtures(
            REVIEW_GATED_REPO,
            PROTECTION_CI_SUMMARY,
            [RULESET_MERGE_QUEUE_DISABLED],
            gql_rules=gql_rules,
        ),
        branches="dev",
    )
    assert result.exit_code == 1, result.report
    assert "approving reviews are enforced (blocks solo-dev merges)" in result.report


def test_main_audit_exempt_repo_skips_main_but_keeps_dev() -> None:
    """A main-exempt repo: main is not audited, dev still is."""
    repo = "omnibase_compat"
    result = run_audit(
        repo,
        _fixtures(repo, PROTECTION_NO_CONTEXTS, [RULESET_MERGE_QUEUE_DISABLED]),
        branches="main,dev",
    )
    assert result.exit_code == 0, result.report
    assert "[main] SKIP: main not audited" in result.report
    assert '[dev] PASS: "CI Summary" is a required status check' in result.report
