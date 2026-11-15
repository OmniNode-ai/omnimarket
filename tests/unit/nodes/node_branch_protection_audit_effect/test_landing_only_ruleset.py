# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A landing branch carries the landing-only ruleset with exactly the declared bypass actors.

The policy declares each landing branch and its bypass actors (the landing App
and the organization admins, both in pull-request mode). The audit reports a
finding when:

* the repository has no active branch ruleset with the update rule on the
  landing branch (a repository not yet switched);
* the ruleset carries a bypass actor the policy does not declare, or lacks one
  it declares;
* the rulesets, or the matching ruleset's detail, cannot be read.

An exact match is a pass, and a repository with no declared landing branch is
not judged.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    EnumBranchProtectionCheckStatus,
    ModelBranchProtectionAuditPolicy,
    ModelBranchProtectionBypassActor,
    ModelBranchProtectionLandingBranch,
)
from tests.nodes.node_branch_protection_audit_effect.audit_fixtures import (
    GQL_RULES,
    ORG,
    POLICY,
    PROTECTION_CI_SUMMARY,
    REPO_SETTINGS,
    RULESET_MERGE_QUEUE_DISABLED,
    RULESET_RESTRICTING_MAIN,
    run_audit,
)

pytestmark = pytest.mark.unit

REPO = "omnibase_infra"
CHECK = "landing_only_ruleset"
LANDING_APP = {
    "actor_id": 4361937,
    "actor_type": "Integration",
    "bypass_mode": "pull_request",
}
ORG_ADMINS = {
    "actor_id": 1,
    "actor_type": "OrganizationAdmin",
    "bypass_mode": "pull_request",
}
EXTRA_ACTOR = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}

LANDING_POLICY = POLICY.model_copy(
    update={
        "landing_branches": (
            ModelBranchProtectionLandingBranch(
                repo=REPO,
                branch="dev",
                bypass_actors=(
                    ModelBranchProtectionBypassActor(
                        actor_type="Integration", actor_id=4361937
                    ),
                    ModelBranchProtectionBypassActor(
                        actor_type="OrganizationAdmin", actor_id=1
                    ),
                ),
            ),
        )
    }
)


def _landing_ruleset(
    actors: list[dict[str, object]], **overrides: object
) -> dict[str, object]:
    ruleset: dict[str, object] = {
        "id": 777,
        "name": "landing-only",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/dev"], "exclude": []}},
        "rules": [{"type": "update"}],
        "bypass_actors": actors,
    }
    ruleset.update(overrides)
    return ruleset


def _fixtures(
    *rulesets: dict[str, object], unreadable: tuple[object, ...] = ()
) -> dict[str, object]:
    base = f"repos/{ORG}/{REPO}"
    all_rulesets = [RULESET_MERGE_QUEUE_DISABLED, RULESET_RESTRICTING_MAIN, *rulesets]
    fx: dict[str, object] = {
        "graphql": GQL_RULES,
        f"{base}/branches/dev/protection": PROTECTION_CI_SUMMARY,
        base: REPO_SETTINGS,
        f"{base}/rulesets": [
            {k: v for k, v in rs.items() if k not in ("rules", "bypass_actors")}
            for rs in all_rulesets
        ],
        f"{base}/rulesets/{RULESET_RESTRICTING_MAIN['id']}": RULESET_RESTRICTING_MAIN,
    }
    for ruleset in rulesets:
        if ruleset["id"] not in unreadable:
            fx[f"{base}/rulesets/{ruleset['id']}"] = ruleset
    return fx


def _landing_findings(
    fixtures: dict[str, object],
    policy: ModelBranchProtectionAuditPolicy = LANDING_POLICY,
) -> list[tuple[EnumBranchProtectionCheckStatus, str]]:
    result = run_audit(REPO, fixtures, branches="dev", policy=policy)
    return [(f.status, f.detail) for f in result.findings if f.check == CHECK]


def test_landing_only_ruleset_declared_and_exact_passes() -> None:
    found = _landing_findings(_fixtures(_landing_ruleset([LANDING_APP, ORG_ADMINS])))
    assert [status for status, _ in found] == [EnumBranchProtectionCheckStatus.PASS]


def test_landing_only_ruleset_missing_is_a_finding() -> None:
    fixtures = _fixtures()
    result = run_audit(REPO, fixtures, branches="dev", policy=LANDING_POLICY)
    found = [(f.status, f.detail) for f in result.findings if f.check == CHECK]
    assert found == [
        (
            EnumBranchProtectionCheckStatus.FAIL,
            "no active ruleset with the update rule",
        )
    ]
    assert result.exit_code == 1
    assert REPO in result.noncompliant_repos


def test_landing_only_ruleset_extra_bypass_actor_is_a_finding() -> None:
    found = _landing_findings(
        _fixtures(_landing_ruleset([LANDING_APP, ORG_ADMINS, EXTRA_ACTOR]))
    )
    assert len(found) == 1
    status, detail = found[0]
    assert status == EnumBranchProtectionCheckStatus.FAIL
    assert "extra bypass RepositoryRole:5:always" in detail
    assert "missing bypass -" in detail


def test_landing_only_ruleset_missing_declared_bypass_actor_is_a_finding() -> None:
    found = _landing_findings(_fixtures(_landing_ruleset([LANDING_APP])))
    assert len(found) == 1
    status, detail = found[0]
    assert status == EnumBranchProtectionCheckStatus.FAIL
    assert "missing bypass OrganizationAdmin:1:pull_request" in detail


def test_landing_only_ruleset_bypass_mode_always_is_a_finding() -> None:
    always_app = {**LANDING_APP, "bypass_mode": "always"}
    found = _landing_findings(_fixtures(_landing_ruleset([always_app, ORG_ADMINS])))
    assert [status for status, _ in found] == [EnumBranchProtectionCheckStatus.FAIL]


@pytest.mark.parametrize(
    "overrides",
    [
        {"enforcement": "disabled"},
        {"enforcement": "evaluate"},
        {"target": "tag"},
        {"rules": [{"type": "non_fast_forward"}]},
        {"conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}}},
        {
            "conditions": {
                "ref_name": {
                    "include": ["refs/heads/dev"],
                    "exclude": ["refs/heads/dev"],
                }
            }
        },
    ],
    ids=["disabled", "evaluate", "tag", "no-update-rule", "other-branch", "excluded"],
)
def test_landing_only_ruleset_that_does_not_restrict_the_branch_is_missing(
    overrides: dict[str, object],
) -> None:
    found = _landing_findings(
        _fixtures(_landing_ruleset([LANDING_APP, ORG_ADMINS], **overrides))
    )
    assert [status for status, _ in found] == [EnumBranchProtectionCheckStatus.FAIL]
    assert found[0][1].startswith("no active ruleset with the update rule")


def test_landing_only_ruleset_unreadable_detail_is_a_finding_that_says_so() -> None:
    ruleset = _landing_ruleset([LANDING_APP, ORG_ADMINS])
    found = _landing_findings(_fixtures(ruleset, unreadable=(ruleset["id"],)))
    assert [status for status, _ in found] == [EnumBranchProtectionCheckStatus.FAIL]
    assert "ruleset(s) 777 could not be read" in found[0][1]


def test_landing_only_ruleset_unfetchable_rulesets_is_a_finding() -> None:
    fixtures = _fixtures(_landing_ruleset([LANDING_APP, ORG_ADMINS]))
    del fixtures[f"repos/{ORG}/{REPO}/rulesets"]
    found = _landing_findings(fixtures)
    assert found == [(EnumBranchProtectionCheckStatus.FAIL, "rulesets not fetchable")]


def test_landing_only_ruleset_not_judged_when_no_landing_branch_is_declared() -> None:
    assert _landing_findings(_fixtures(), policy=POLICY) == []


def test_landing_only_ruleset_control_both_fixtures_audit_clean_without_the_policy() -> (
    None
):
    """Failing control: with no landing branch declared, neither fixture is found."""
    for actors in ([LANDING_APP, ORG_ADMINS, EXTRA_ACTOR], []):
        assert _landing_findings(_fixtures(), policy=POLICY) == []
        assert (
            _landing_findings(_fixtures(_landing_ruleset(actors)), policy=POLICY) == []
        )


def test_landing_only_ruleset_one_landing_branch_per_repository() -> None:
    entry = ModelBranchProtectionLandingBranch(repo=REPO, branch="dev")
    with pytest.raises(ValueError, match="one landing branch only"):
        ModelBranchProtectionAuditPolicy(
            landing_branches=(entry, entry.model_copy(update={"branch": "main"}))
        )
