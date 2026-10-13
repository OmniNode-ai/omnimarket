# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fetch boundary reads every document the audit needs, and records failures.

The reader is the only GitHub access of the node. These tests give it a fake
reader so they run on fixtures: the fetch asks for the review rules, the
protection of each audited branch, the repository settings, the rulesets list
and the detail of every ruleset that is not disabled, and turns a failed read
into an absent document with its error text.
"""

from __future__ import annotations

import pytest
from pydantic import JsonValue

from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_fetch import (
    HandlerBranchProtectionFetch,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    ModelBranchProtectionAuditPolicy,
    ModelBranchProtectionFetchRequest,
    protection_key,
    repo_key,
    review_rules_key,
    ruleset_key,
    rulesets_key,
)
from tests.nodes.node_branch_protection_audit_effect.audit_fixtures import (
    GQL_RULES,
    ORG,
    PROTECTION_CI_SUMMARY,
    REPO_SETTINGS,
    RULESET_MERGE_QUEUE_DISABLED,
    RULESET_RESTRICTING_MAIN,
)

pytestmark = pytest.mark.unit


class _FakeReader:
    def __init__(self, rest: dict[str, JsonValue]) -> None:
        self.rest = rest
        self.rest_calls: list[str] = []
        self.graphql_calls: list[dict[str, str]] = []

    def read_rest(self, path: str) -> JsonValue:
        self.rest_calls.append(path)
        if path not in self.rest:
            raise GitHubApiError(f"Not Found: {path}", status_code=404)
        return self.rest[path]

    def read_graphql(self, query: str, variables: dict[str, str]) -> JsonValue:
        assert "branchProtectionRules" in query
        self.graphql_calls.append(variables)
        data = GQL_RULES["data"]
        assert isinstance(data, dict)
        return data


def _fetch(
    reader: _FakeReader, policy: ModelBranchProtectionAuditPolicy, branches: str
) -> object:
    return HandlerBranchProtectionFetch(reader=reader).handle(
        ModelBranchProtectionFetchRequest(
            policy=policy, org=ORG, branches=tuple(branches.split(","))
        )
    )


def test_fetch_reads_every_document_the_audit_judges() -> None:
    repo = "repo_a"
    rest: dict[str, JsonValue] = {
        protection_key(ORG, repo, "main"): PROTECTION_CI_SUMMARY,
        protection_key(ORG, repo, "dev"): PROTECTION_CI_SUMMARY,
        repo_key(ORG, repo): REPO_SETTINGS,
        rulesets_key(ORG, repo): [
            {"id": 13269695, "enforcement": "disabled"},
            {"id": 21864685, "enforcement": "active"},
        ],
        ruleset_key(ORG, repo, 21864685): RULESET_RESTRICTING_MAIN,
    }
    reader = _FakeReader(rest)
    snapshot = HandlerBranchProtectionFetch(reader=reader).handle(
        ModelBranchProtectionFetchRequest(
            policy=ModelBranchProtectionAuditPolicy(repos=(repo,)),
            org=ORG,
            branches=("main", "dev"),
        )
    )
    assert reader.graphql_calls == [{"owner": ORG, "name": repo}]
    assert snapshot.documents[review_rules_key(ORG, repo)] == GQL_RULES
    for key in rest:
        assert snapshot.documents[key] == rest[key]
    # The disabled ruleset is never judged, so its detail is never read.
    assert ruleset_key(ORG, repo, 13269695) not in reader.rest_calls
    assert snapshot.errors == {}


def test_failed_reads_are_absent_documents_with_their_error() -> None:
    repo = "repo_a"
    reader = _FakeReader({repo_key(ORG, repo): REPO_SETTINGS})
    snapshot = HandlerBranchProtectionFetch(reader=reader).handle(
        ModelBranchProtectionFetchRequest(
            policy=ModelBranchProtectionAuditPolicy(repos=(repo,)),
            org=ORG,
            branches=("dev",),
        )
    )
    key = protection_key(ORG, repo, "dev")
    assert key not in snapshot.documents
    assert snapshot.errors[key] == f"Not Found: {key}"
    assert rulesets_key(ORG, repo) in snapshot.errors


def test_exempt_branches_are_not_read() -> None:
    reader = _FakeReader({})
    HandlerBranchProtectionFetch(reader=reader).handle(
        ModelBranchProtectionFetchRequest(
            policy=ModelBranchProtectionAuditPolicy(
                repos=("main_exempt", "dev_exempt"),
                main_audit_exempt_repos=("main_exempt",),
                dev_exempt_repos=("dev_exempt",),
            ),
            org=ORG,
            branches=("main", "dev"),
        )
    )
    assert protection_key(ORG, "main_exempt", "main") not in reader.rest_calls
    assert protection_key(ORG, "main_exempt", "dev") in reader.rest_calls
    assert protection_key(ORG, "dev_exempt", "dev") not in reader.rest_calls
    assert protection_key(ORG, "dev_exempt", "main") in reader.rest_calls


def test_fetched_snapshot_judges_like_the_fixture_snapshot() -> None:
    """The fetch and the judge agree on keys: a fetched snapshot audits clean."""
    from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_audit import (
        HandlerBranchProtectionAudit,
    )
    from omnimarket.nodes.node_branch_protection_audit_effect.models import (
        ModelBranchProtectionAuditRequest,
    )

    repo = "repo_a"
    policy = ModelBranchProtectionAuditPolicy(repos=(repo,))
    reader = _FakeReader(
        {
            protection_key(ORG, repo, "dev"): PROTECTION_CI_SUMMARY,
            repo_key(ORG, repo): REPO_SETTINGS,
            rulesets_key(ORG, repo): [RULESET_MERGE_QUEUE_DISABLED],
        }
    )
    snapshot = HandlerBranchProtectionFetch(reader=reader).handle(
        ModelBranchProtectionFetchRequest(policy=policy, org=ORG, branches=("dev",))
    )
    result = HandlerBranchProtectionAudit().handle(
        ModelBranchProtectionAuditRequest(
            policy=policy, snapshot=snapshot, org=ORG, branches=("dev",)
        )
    )
    assert result.exit_code == 0, result.report
    assert result.total_checks == 7
