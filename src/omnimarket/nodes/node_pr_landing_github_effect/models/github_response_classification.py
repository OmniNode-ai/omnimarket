# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Map a GitHub response to a typed failure reason, or None (OMN-19826).

Pure and deterministic: status, x-ratelimit-remaining, retry-after and the
error message decide the reason. No clock, no I/O.
"""

from __future__ import annotations

from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_failure_reason import (
    EnumPrLandingGithubFailureReason,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.model_github_http_exchange import (
    ModelGithubHttpResponse,
)

# Markers mirrored from node_pr_lifecycle_merge_effect adapter_github_merge_queue.
_NO_MERGE_QUEUE_MARKERS = (
    "does not have a merge queue",
    "merge queue is not enabled",
    "merge_queue_not_enabled",
)
_AUTO_MERGE_NOT_ALLOWED_MARKERS = (
    "auto merge is not allowed",
    "auto-merge is not allowed",
)


def classify_github_response(
    operation: EnumPrLandingGithubOperation, response: ModelGithubHttpResponse
) -> EnumPrLandingGithubFailureReason | None:
    """Return the failure reason, or None when the response is a success.

    A 304 is a success only for read_head_checks (the conditional read).
    """
    status = response.status
    message = response.message().lower()
    if status == 304:
        if operation is EnumPrLandingGithubOperation.READ_HEAD_CHECKS:
            return None
        return EnumPrLandingGithubFailureReason.VALIDATION_FAILED
    if 200 <= status < 300:
        errors = (response.body or {}).get("errors")
        if not errors:
            return None
        if any(m in message for m in _AUTO_MERGE_NOT_ALLOWED_MARKERS):
            return EnumPrLandingGithubFailureReason.AUTO_MERGE_NOT_ALLOWED
        if any(m in message for m in _NO_MERGE_QUEUE_MARKERS):
            return EnumPrLandingGithubFailureReason.MERGE_QUEUE_NOT_ENABLED
        return EnumPrLandingGithubFailureReason.GRAPHQL_ERROR
    if status in (403, 429):
        if response.header("x-ratelimit-remaining") == "0":
            return EnumPrLandingGithubFailureReason.PRIMARY_RATE_LIMIT
        if "secondary rate limit" in message or response.header("retry-after"):
            return EnumPrLandingGithubFailureReason.SECONDARY_RATE_LIMIT
        return EnumPrLandingGithubFailureReason.FORBIDDEN
    if status == 401:
        return EnumPrLandingGithubFailureReason.FORBIDDEN
    if status == 404:
        return EnumPrLandingGithubFailureReason.NOT_FOUND
    if status == 422:
        if operation is EnumPrLandingGithubOperation.UPDATE_BRANCH:
            if "merge conflict" in message:
                return EnumPrLandingGithubFailureReason.UPDATE_BRANCH_CONFLICT
            if "expected head sha" in message:
                return EnumPrLandingGithubFailureReason.HEAD_MOVED
        return EnumPrLandingGithubFailureReason.VALIDATION_FAILED
    if status >= 500:
        return EnumPrLandingGithubFailureReason.SERVER_ERROR
    return EnumPrLandingGithubFailureReason.VALIDATION_FAILED
