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

    ``DRAFT``, ``HELD``, ``PR_NOT_OPEN`` and ``MERGE_QUEUE_REQUIRED`` are
    refusals an arm or enqueue makes after its own read, before any mutation.
    A head that moved past the expected head is ``HEAD_MOVED``.
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
    # Contract 1.4.0 (OMN-23777): refusals made before any request. A mutation is
    # sent only with the writer App's repository-scoped installation token. No
    # such token is the first; a token lacking pull request or contents write,
    # or not narrowed to the one repository, is the second.
    LANDING_IDENTITY_NOT_APP = "landing_identity_not_app"
    MINTED_PERMISSIONS_INSUFFICIENT = "minted_permissions_insufficient"
