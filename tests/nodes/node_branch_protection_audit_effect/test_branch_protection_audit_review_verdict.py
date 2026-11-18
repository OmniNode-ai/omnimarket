# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review verdict context is a required status check on the listed branches.

Copied from the Lane Identity Gate test: a declared requirement is asserted on
the listed (repo, branch) only, and a repository outside the list is not
asserted. The policy refuses requirements with no context, and the overlay
loader loads the two keys from a YAML file and refuses a malformed requirement.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_policy_load import (
    BranchProtectionOverlayError,
    HandlerBranchProtectionPolicyLoad,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    ModelBranchProtectionAuditPolicy,
    ModelBranchProtectionPolicyLoadRequest,
    ModelBranchProtectionReviewVerdictRequirement,
)
from tests.nodes.node_branch_protection_audit_effect.audit_fixtures import (
    GQL_RULES,
    ORG,
    REPO_SETTINGS,
    RULESET_MERGE_QUEUE_DISABLED,
    RULESET_RESTRICTING_MAIN,
    run_audit,
)

pytestmark = pytest.mark.unit

REVIEW_VERDICT_CONTEXT = "review / verdict"
REVIEW_VERDICT_REPO = "omnibase_core"
OTHER_REPO = "fixture-unlisted-repo"

_DEV_WITH_REVIEW_VERDICT: dict[str, object] = {
    "required_status_checks": {
        "strict": False,
        "contexts": [
            "CI Summary",
            "verify / verify",
            "Lane Identity Gate",
            "review / verdict",
        ],
        "checks": [
            {"context": "CI Summary", "app_id": 15368},
            {"context": "verify / verify", "app_id": 15368},
            {"context": "Lane Identity Gate", "app_id": 15368},
            {"context": "review / verdict", "app_id": 15368},
        ],
    },
    "enforce_admins": {"enabled": True},
}
_DEV_WITHOUT_REVIEW_VERDICT: dict[str, object] = {
    "required_status_checks": {
        "strict": False,
        "contexts": ["CI Summary", "verify / verify", "Lane Identity Gate"],
        "checks": [
            {"context": "CI Summary", "app_id": 15368},
            {"context": "verify / verify", "app_id": 15368},
            {"context": "Lane Identity Gate", "app_id": 15368},
        ],
    },
    "enforce_admins": {"enabled": True},
}
_MAIN_RELEASE_SYNCED: dict[str, object] = {
    "required_status_checks": {"strict": False, "contexts": [], "checks": []},
    "enforce_admins": {"enabled": True},
}


def _fixtures(repo: str, dev_protection: dict[str, object]) -> dict[str, object]:
    base = f"repos/{ORG}/{repo}"
    rulesets = [RULESET_MERGE_QUEUE_DISABLED, RULESET_RESTRICTING_MAIN]
    fx: dict[str, object] = {
        "graphql": GQL_RULES,
        f"{base}/branches/main/protection": _MAIN_RELEASE_SYNCED,
        f"{base}/branches/dev/protection": dev_protection,
        base: REPO_SETTINGS,
        f"{base}/rulesets": rulesets,
    }
    for rs in rulesets:
        fx[f"{base}/rulesets/{rs['id']}"] = rs
    return fx


def _overrides(context: str = REVIEW_VERDICT_CONTEXT) -> dict:
    return {
        "review_verdict_context": context,
        "review_verdict_required": (
            ModelBranchProtectionReviewVerdictRequirement(
                repo=REVIEW_VERDICT_REPO, branch="dev"
            ),
        ),
    }


def test_dev_with_review_verdict_passes() -> None:
    result = run_audit(
        REVIEW_VERDICT_REPO,
        _fixtures(REVIEW_VERDICT_REPO, _DEV_WITH_REVIEW_VERDICT),
        branches="dev",
        policy_overrides=_overrides(),
    )
    assert result.exit_code == 0, result.report
    assert '"review / verdict" is a required status check' in result.report


def test_dev_missing_review_verdict_fails() -> None:
    result = run_audit(
        REVIEW_VERDICT_REPO,
        _fixtures(REVIEW_VERDICT_REPO, _DEV_WITHOUT_REVIEW_VERDICT),
        branches="dev",
        policy_overrides=_overrides(),
    )
    assert result.exit_code == 1, result.report
    assert '"review / verdict" not found in required status checks' in result.report
    assert "a pull request can merge with no clean review verdict" in result.report


def test_review_verdict_is_not_asserted_on_main() -> None:
    result = run_audit(
        REVIEW_VERDICT_REPO,
        _fixtures(REVIEW_VERDICT_REPO, _DEV_WITH_REVIEW_VERDICT),
        branches="main",
        policy_overrides=_overrides(),
    )
    assert result.exit_code == 0, result.report
    assert "review / verdict" not in result.report


def test_a_repo_outside_the_list_is_not_asserted() -> None:
    result = run_audit(
        OTHER_REPO,
        _fixtures(OTHER_REPO, _DEV_WITHOUT_REVIEW_VERDICT),
        branches="dev",
        policy_overrides=_overrides(),
    )
    assert result.exit_code == 0, result.report
    assert "review / verdict" not in result.report


def test_policy_refuses_requirements_with_no_context() -> None:
    with pytest.raises(ValueError, match="review_verdict_context"):
        ModelBranchProtectionAuditPolicy(
            review_verdict_required=(
                ModelBranchProtectionReviewVerdictRequirement(
                    repo=REVIEW_VERDICT_REPO, branch="dev"
                ),
            ),
        )


def test_overlay_loader_loads_review_verdict_keys(tmp_path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "schema_version: branch_protection_audit_overlay.v1\n"
        "review_verdict_context: review / verdict\n"
        "review_verdict_required:\n"
        "  - repo: omniclaude\n"
        "    branch: dev\n",
        encoding="utf-8",
    )
    policy = HandlerBranchProtectionPolicyLoad().handle(
        ModelBranchProtectionPolicyLoadRequest(overlay_path=overlay)
    )
    assert policy.review_verdict_context == "review / verdict"
    assert policy.review_verdict_required == (
        ModelBranchProtectionReviewVerdictRequirement(repo="omniclaude", branch="dev"),
    )


def test_overlay_loader_refuses_a_malformed_requirement(tmp_path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "schema_version: branch_protection_audit_overlay.v1\n"
        "review_verdict_context: review / verdict\n"
        "review_verdict_required:\n"
        "  - repo: omniclaude\n",
        encoding="utf-8",
    )
    with pytest.raises(BranchProtectionOverlayError):
        HandlerBranchProtectionPolicyLoad().handle(
            ModelBranchProtectionPolicyLoadRequest(overlay_path=overlay)
        )


def test_injected_break_reads_red_while_matching_name_reads_green() -> None:
    green = run_audit(
        REVIEW_VERDICT_REPO,
        _fixtures(REVIEW_VERDICT_REPO, _DEV_WITH_REVIEW_VERDICT),
        branches="dev",
        policy_overrides=_overrides(),
    )
    assert green.exit_code == 0, green.report
    assert '"review / verdict" is a required status check' in green.report
    red = run_audit(
        REVIEW_VERDICT_REPO,
        _fixtures(REVIEW_VERDICT_REPO, _DEV_WITH_REVIEW_VERDICT),
        branches="dev",
        policy_overrides=_overrides(context="review / verdict (injected)"),
    )
    assert red.exit_code == 1, red.report
    assert (
        '"review / verdict (injected)" not found in required status checks'
        in red.report
    )
