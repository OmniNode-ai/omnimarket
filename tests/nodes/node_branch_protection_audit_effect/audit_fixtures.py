# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the branch-protection audit tests.

Ported from onex_change_control's ``tests/test_audit_branch_protection_*.py``,
which drove ``scripts/audit_branch_protection.sh`` against a stubbed ``gh``
answering from a dict keyed by API path (and the literal ``graphql``). Here the
same dict becomes the snapshot the audit handler judges: a path the dict does
not carry is a failed read, exactly as the stub exited non-zero for it.

The policy below stands in for the arrays the script carried. The script's one
ordinary (not release-synced) main was a private repository; the tests name it
``ordinary_main_repo`` and list it as private, which is the role that mattered.
"""

from __future__ import annotations

from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_audit import (
    HandlerBranchProtectionAudit,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    DEFAULT_ORG,
    ModelBranchProtectionAuditPolicy,
    ModelBranchProtectionAuditRequest,
    ModelBranchProtectionAuditResult,
    ModelBranchProtectionSnapshot,
    review_rules_key,
)

ORG = DEFAULT_ORG
ORDINARY_REPO = "ordinary_main_repo"

POLICY = ModelBranchProtectionAuditPolicy(
    private_repos=(ORDINARY_REPO,),
    release_synced_main_repos=(
        "omnibase_core",
        "omnibase_infra",
        "omnibase_spi",
        "omnimemory",
        "omnidash",
        "omniintelligence",
        "omnimarket",
        "omniclaude",
    ),
    main_audit_exempt_repos=("omnibase_compat",),
    review_gated_main_repos=("onex_change_control",),
    receipt_gate_required_repos=(
        "omniclaude",
        "omnimarket",
        "omnibase_compat",
        "omnibase_core",
        "omnibase_infra",
        "omniintelligence",
        ORDINARY_REPO,
        "onex_change_control",
    ),
    lane_identity_gate_required_repos=("omniclaude",),
)

PROTECTION_NO_CONTEXTS: dict[str, object] = {
    "required_status_checks": {"strict": False, "contexts": [], "checks": []},
    "enforce_admins": {"enabled": True},
}
PROTECTION_CI_SUMMARY: dict[str, object] = {
    "required_status_checks": {
        "strict": False,
        "contexts": ["CI Summary", "verify / verify"],
        "checks": [
            {"context": "CI Summary", "app_id": 15368},
            {"context": "verify / verify", "app_id": 15368},
        ],
    },
    "enforce_admins": {"enabled": True},
}
REPO_SETTINGS: dict[str, object] = {"delete_branch_on_merge": True}
GQL_RULES: dict[str, object] = {
    "data": {
        "repository": {
            "branchProtectionRules": {
                "nodes": [
                    {
                        "pattern": "main",
                        "requiresApprovingReviews": False,
                        "requiresCodeOwnerReviews": False,
                    },
                    {
                        "pattern": "dev",
                        "requiresApprovingReviews": False,
                        "requiresCodeOwnerReviews": False,
                    },
                ]
            }
        }
    }
}
GQL_REVIEW_GATED_MAIN: dict[str, object] = {
    "data": {
        "repository": {
            "branchProtectionRules": {
                "nodes": [
                    {
                        "pattern": "main",
                        "requiresApprovingReviews": True,
                        "requiresCodeOwnerReviews": True,
                    },
                    {
                        "pattern": "dev",
                        "requiresApprovingReviews": False,
                        "requiresCodeOwnerReviews": False,
                    },
                ]
            }
        }
    }
}
# An active ruleset that genuinely restricts who may advance refs/heads/main.
RULESET_RESTRICTING_MAIN: dict[str, object] = {
    "id": 21864685,
    "name": "release-synced main: restrict pushes to release automation",
    "target": "branch",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
    "rules": [{"type": "update"}, {"type": "non_fast_forward"}],
    "bypass_actors": [
        {"actor_id": 4361937, "actor_type": "Integration", "bypass_mode": "always"}
    ],
}
# Present in most fixtures: the disabled legacy Merge Queue ruleset. The
# repo-level "Merge Queue ruleset exists" check matches on NAME only, so this
# must never be mistaken for the push restriction.
RULESET_MERGE_QUEUE_DISABLED: dict[str, object] = {
    "id": 13269695,
    "name": "Merge Queue",
    "target": "branch",
    "enforcement": "disabled",
    "conditions": {"ref_name": {"include": ["refs/heads/dev"], "exclude": []}},
    "rules": [{"type": "merge_queue"}],
    "bypass_actors": [],
}


def snapshot(repo: str, fixtures: dict[str, object]) -> ModelBranchProtectionSnapshot:
    """The stub-``gh`` fixture dict as a snapshot; ``graphql`` maps to the repo's key."""
    documents = {
        (review_rules_key(ORG, repo) if key == "graphql" else key): value
        for key, value in fixtures.items()
    }
    return ModelBranchProtectionSnapshot.model_validate({"documents": documents})


def run_audit(
    repo: str,
    fixtures: dict[str, object],
    branches: str = "main",
    policy: ModelBranchProtectionAuditPolicy = POLICY,
) -> ModelBranchProtectionAuditResult:
    """Judge one repository, as the script did with BRANCH_PROTECTION_AUDIT_REPOS=repo."""
    return HandlerBranchProtectionAudit().handle(
        ModelBranchProtectionAuditRequest(
            policy=policy.model_copy(update={"repos": (repo,)}),
            snapshot=snapshot(repo, fixtures),
            org=ORG,
            branches=tuple(branches.split(",")),
        )
    )


def review_rule(gql_rules: dict[str, object], index: int) -> dict[str, object]:
    """The index-th branchProtectionRules node of a GraphQL fixture, for editing."""
    data = gql_rules["data"]
    assert isinstance(data, dict)
    nodes = data["repository"]["branchProtectionRules"]["nodes"]
    assert isinstance(nodes, list)
    node = nodes[index]
    assert isinstance(node, dict)
    return node
