# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed failure reasons of node_pr_landing_github_effect (OMN-19826)."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingGithubFailureReason(StrEnum):
    """Why an operation did not complete.

    ``QUOTA_FLOOR`` is the effect's own refusal: the last header reading for the
    call's resource was under the contract's floor, so no call was made.
    ``TRANSPORT_ERROR`` is the only reason with no GitHub response behind it.

    ``DRAFT``, ``HELD``, ``PR_NOT_OPEN`` and ``MERGE_QUEUE_REQUIRED`` (contract
    1.1.0, OMN-19831) are refusals an arm or enqueue makes after its own read,
    before any mutation: the PR is draft, carries a hold marker, is closed or
    merged, or its base branch has a merge queue so the policy is enqueue, not
    arm. A head that moved past the expected head is ``HEAD_MOVED``.
    """

    QUOTA_FLOOR = "quota_floor"
    PRIMARY_RATE_LIMIT = "primary_rate_limit"
    SECONDARY_RATE_LIMIT = "secondary_rate_limit"
    UPDATE_BRANCH_CONFLICT = "update_branch_conflict"
    HEAD_MOVED = "head_moved"
    AUTO_MERGE_NOT_ALLOWED = "auto_merge_not_allowed"
    MERGE_QUEUE_NOT_ENABLED = "merge_queue_not_enabled"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    VALIDATION_FAILED = "validation_failed"
    GRAPHQL_ERROR = "graphql_error"
    SERVER_ERROR = "server_error"
    TRANSPORT_ERROR = "transport_error"
    DRAFT = "draft"
    HELD = "held"
    PR_NOT_OPEN = "pr_not_open"
    MERGE_QUEUE_REQUIRED = "merge_queue_required"
