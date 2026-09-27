# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Map a GitHub response to a typed failure reason, or None (OMN-19826).

Pure and deterministic: status, x-ratelimit-remaining, retry-after and the
error message decide the reason. No clock, no I/O.
"""

from __future__ import annotations

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_failure_reason import (
    EnumPrLandingGithubFailureReason,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
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
# A mutation carrying expectedHeadOid on a moved head (contract 1.1.0, R4).
# Documented wording, not recorded: recording it needs a mutating call.
_HEAD_MOVED_MARKERS = (
    "head branch was modified",
    "expected head",
    "expectedheadoid",
    "head sha didn't match",
)
_CONDITIONAL_READS = frozenset(
    {
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        EnumPrLandingGithubOperation.READ_PR_STATE,
    }
)


def classify_github_response(
    operation: EnumPrLandingGithubOperation, response: ModelGithubHttpResponse
) -> EnumPrLandingGithubFailureReason | None:
    """Return the failure reason, or None when the response is a success.

    A 304 is a success only for the conditional reads, read_head_checks and
    read_pr_state.
    """
    status = response.status
    message = response.message().lower()
    if status == 304:
        if operation in _CONDITIONAL_READS:
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
        if any(m in message for m in _HEAD_MOVED_MARKERS):
            return EnumPrLandingGithubFailureReason.HEAD_MOVED
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
