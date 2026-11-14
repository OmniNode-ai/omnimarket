# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reason rules of ModelPrLandingGithubFailed (OMN-19826, OMN-23777)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnimarket.events.pr_landing_github.enum_pr_landing_github_failure_reason import (
    EnumPrLandingGithubFailureReason as Reason,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)

if TYPE_CHECKING:
    from omnimarket.events.pr_landing_github.model_pr_landing_github_failed import (
        ModelPrLandingGithubFailed,
    )

NO_REQUEST_REASONS = frozenset(
    {Reason.LANDING_IDENTITY_NOT_APP, Reason.MINTED_PERMISSIONS_INSUFFICIENT}
)


def check_failure_reason_rules(f: ModelPrLandingGithubFailed) -> None:
    """Raise ValueError when a failure's fields do not fit its reason."""
    if f.mode is EnumPrLandingGithubMode.DRY_RUN:
        raise ValueError("dry_run records requests and calls nothing; it cannot fail")
    no_response = f.reason is Reason.TRANSPORT_ERROR
    # OMN-23777: an identity refusal is made before any request, so it has
    # no response, no status and no quota reading behind it either.
    no_request = f.reason in NO_REQUEST_REASONS
    if f.quota is None and not no_response and not no_request:
        raise ValueError(
            f"a {f.reason.value} failure must carry the header quota reading"
        )
    if f.reason is Reason.QUOTA_FLOOR and f.http_status is not None:
        raise ValueError("a quota_floor refusal made no call and has no http_status")
    if no_response and f.http_status is not None:
        raise ValueError("a transport_error has no http_status")
    if no_request and (f.http_status is not None or f.quota is not None):
        raise ValueError(
            f"a {f.reason.value} refusal made no request and carries neither "
            "an http_status nor a quota reading"
        )
    if f.pr_state is not None and f.operation not in (
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
        EnumPrLandingGithubOperation.ENQUEUE,
    ):
        raise ValueError("pr_state is only carried by a refused arm or enqueue")
    if (
        f.head_sha is None
        and f.operation is not EnumPrLandingGithubOperation.READ_PR_STATE
    ):
        raise ValueError(f"a {f.operation.value} failure must name its head_sha")
