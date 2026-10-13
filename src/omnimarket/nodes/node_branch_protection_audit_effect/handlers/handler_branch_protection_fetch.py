# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Collect the documents the pure branch-protection auditor judges."""

import json

from pydantic import JsonValue

from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_github_api_read import (
    GitHubTokenResolutionError,
    HandlerGitHubApiRead,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    ModelBranchProtectionFetchRequest,
    ModelBranchProtectionSnapshot,
    protection_key,
    repo_key,
    review_rules_key,
    ruleset_key,
    rulesets_key,
)
from omnimarket.nodes.node_branch_protection_audit_effect.protocols.protocol_github_api_read import (
    ProtocolGitHubApiRead,
)

_REVIEW_QUERY = (
    "query($owner:String!,$name:String!){ repository(owner:$owner,name:$name){ "
    "branchProtectionRules(first:50){ nodes{ pattern requiresApprovingReviews "
    "requiresCodeOwnerReviews } } } }"
)


class HandlerBranchProtectionFetch:
    """Read each repository in policy order and retain failed reads as errors."""

    def __init__(self, reader: ProtocolGitHubApiRead | None = None) -> None:
        self._reader = reader if reader is not None else HandlerGitHubApiRead()

    def _read_rest(
        self, key: str, documents: dict[str, JsonValue], errors: dict[str, str]
    ) -> None:
        try:
            documents[key] = self._reader.read_rest(key)
        except GitHubTokenResolutionError:
            raise
        except GitHubApiError as exc:
            errors[key] = str(exc)

    def handle(
        self, request: ModelBranchProtectionFetchRequest
    ) -> ModelBranchProtectionSnapshot:
        documents: dict[str, JsonValue] = {}
        errors: dict[str, str] = {}
        for repo in request.policy.repos:
            key = review_rules_key(request.org, repo)
            try:
                documents[key] = {
                    "data": self._reader.read_graphql(
                        _REVIEW_QUERY, {"owner": request.org, "name": repo}
                    )
                }
            except GitHubTokenResolutionError:
                raise
            except GitHubApiError as exc:
                errors[key] = str(exc)
            for branch in request.branches:
                if branch == "main" and repo in request.policy.main_audit_exempt_repos:
                    continue
                if branch == "dev" and repo in request.policy.dev_exempt_repos:
                    continue
                self._read_rest(
                    protection_key(request.org, repo, branch), documents, errors
                )
            self._read_rest(repo_key(request.org, repo), documents, errors)
            key = rulesets_key(request.org, repo)
            self._read_rest(key, documents, errors)
            listed = documents.get(key)
            if isinstance(listed, list):
                for ruleset in listed:
                    if (
                        not isinstance(ruleset, dict)
                        or ruleset.get("enforcement") == "disabled"
                    ):
                        continue
                    rid = ruleset.get("id")
                    # jq prints a missing identifier as the literal "null".
                    identifier = (
                        rid
                        if isinstance(rid, str)
                        else json.dumps(rid, ensure_ascii=False, separators=(",", ":"))
                    )
                    self._read_rest(
                        ruleset_key(request.org, repo, identifier), documents, errors
                    )
        return ModelBranchProtectionSnapshot(documents=documents, errors=errors)
