# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operation dispatcher for node_github_repo_gateway_effect.

Routes a validated request to exactly one read function and returns the
discriminated typed result. The dispatcher owns no read logic of its own — it
only maps ``operation`` to a read function, so the dispatch output is identical
to calling that read function directly.
"""

from __future__ import annotations

from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    GithubGatewayResult,
    ModelGithubGatewayRequest,
)
from omnimarket.nodes.node_github_repo_gateway_effect.read_operations import (
    read_branch_protection,
    read_ci_checks,
    read_merge_commit_sha,
    read_open_prs_list,
    read_pr_status,
    read_review_gate,
    read_ticket_ref,
)
from omnimarket.nodes.node_github_repo_gateway_effect.transport import (
    GitHubReadTransportProtocol,
)


def dispatch(
    request: ModelGithubGatewayRequest,
    transport: GitHubReadTransportProtocol,
) -> GithubGatewayResult:
    """Route a request to its read function and return the typed result."""
    op = request.operation
    if op is EnumGithubGatewayOperation.OPEN_PRS_LIST:
        return read_open_prs_list(transport, request.repo)
    if op is EnumGithubGatewayOperation.BRANCH_PROTECTION:
        return read_branch_protection(transport, request.repo)

    # PR-scoped operations: the request validator guarantees pr_number is set.
    pr_number = request.pr_number
    if pr_number is None:  # defensive; validator already enforces this
        raise ValueError(f"operation {op.value!r} requires pr_number.")

    if op is EnumGithubGatewayOperation.PR_STATUS:
        return read_pr_status(transport, request.repo, pr_number)
    if op is EnumGithubGatewayOperation.CI_CHECKS:
        return read_ci_checks(transport, request.repo, pr_number)
    if op is EnumGithubGatewayOperation.REVIEW_GATE:
        return read_review_gate(transport, request.repo, pr_number)
    if op is EnumGithubGatewayOperation.MERGE_COMMIT_SHA:
        return read_merge_commit_sha(transport, request.repo, pr_number)
    if op is EnumGithubGatewayOperation.TICKET_REF:
        return read_ticket_ref(transport, request.repo, pr_number)

    raise ValueError(f"unhandled operation: {op!r}")  # pragma: no cover


__all__: list[str] = ["dispatch"]
