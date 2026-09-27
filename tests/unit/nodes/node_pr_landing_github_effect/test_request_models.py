# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command and result model rules for node_pr_landing_github_effect (OMN-19826)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelGithubQuotaReading,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubFailed,
    ModelPrLandingGithubRequest,
)

pytestmark = pytest.mark.unit

_HEAD = "c5dea513fd94a0e940938f13a798c685ab7a88cb"
_NODE = "PR_kwDOR6jjtc8AAAABFQhbqA"
_BASE: dict[str, Any] = {
    "correlation_id": UUID("00000000-0000-4000-8000-000000019826"),
    "mode": EnumPrLandingGithubMode.ENFORCE,
    "repository": "OmniNode-ai/omnimarket",
    "pr_number": 2962,
    "head_sha": _HEAD,
}
_QUOTA = ModelGithubQuotaReading(
    limit=5000,
    remaining=4000,
    used=1000,
    reset=1790468175,
    resource="core",
    identity="GITHUB_TOKEN",
)


def _cmd(**kw: Any) -> ModelPrLandingGithubRequest:
    return ModelPrLandingGithubRequest.model_validate({**_BASE, **kw})


def test_rerun_requires_named_run_ids() -> None:
    with pytest.raises(ValidationError, match="run_ids"):
        _cmd(operation=EnumPrLandingGithubOperation.RERUN_RUNS)
    with pytest.raises(ValidationError, match="run_ids"):
        _cmd(operation=EnumPrLandingGithubOperation.RERUN_RUNS, run_ids=[1, 1])
    assert (
        len(
            _cmd(
                operation=EnumPrLandingGithubOperation.RERUN_RUNS, run_ids=[1, 2]
            ).to_http_requests()
        )
        == 2
    )


def test_run_ids_are_refused_on_other_operations() -> None:
    with pytest.raises(ValidationError, match="run_ids"):
        _cmd(operation=EnumPrLandingGithubOperation.UPDATE_BRANCH, run_ids=[1])


@pytest.mark.parametrize(
    "operation",
    [
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
        EnumPrLandingGithubOperation.ENQUEUE,
        EnumPrLandingGithubOperation.DISARM,
    ],
)
def test_graphql_operations_require_the_pr_node_id(
    operation: EnumPrLandingGithubOperation,
) -> None:
    extra = (
        {"armed_method": "auto_merge"}
        if operation is EnumPrLandingGithubOperation.DISARM
        else {}
    )
    with pytest.raises(ValidationError, match="pr_node_id"):
        _cmd(operation=operation, **extra)
    request = _cmd(operation=operation, pr_node_id=_NODE, **extra).to_http_requests()[0]
    assert request.path == "/graphql"
    assert request.method == "POST"


def test_disarm_requires_the_armed_method() -> None:
    with pytest.raises(ValidationError, match="armed_method"):
        _cmd(operation=EnumPrLandingGithubOperation.DISARM, pr_node_id=_NODE)


def test_etag_only_on_read_head_checks() -> None:
    with pytest.raises(ValidationError, match="etag"):
        _cmd(operation=EnumPrLandingGithubOperation.UPDATE_BRANCH, etag='W/"x"')
    read = _cmd(operation=EnumPrLandingGithubOperation.READ_HEAD_CHECKS, etag='W/"x"')
    assert read.to_http_requests()[0].if_none_match == 'W/"x"'


@pytest.mark.parametrize("repository", ["omnimarket", "a/b/c", "/x", "x/"])
def test_repository_must_be_owner_slash_name(repository: str) -> None:
    with pytest.raises(ValidationError):
        _cmd(
            operation=EnumPrLandingGithubOperation.UPDATE_BRANCH, repository=repository
        )


def test_head_sha_must_be_a_full_sha() -> None:
    with pytest.raises(ValidationError):
        _cmd(operation=EnumPrLandingGithubOperation.UPDATE_BRANCH, head_sha="c5dea51")


def _completed(**kw: Any) -> ModelPrLandingGithubCompleted:
    cmd = _cmd(operation=EnumPrLandingGithubOperation.UPDATE_BRANCH)
    data: dict[str, Any] = {
        **_BASE,
        "operation": EnumPrLandingGithubOperation.UPDATE_BRANCH,
        "requests": cmd.to_http_requests(),
        "http_statuses": (202,),
        "not_modified": False,
        "etag": None,
        "check_runs": (),
        "quota": _QUOTA,
    }
    data.update(kw)
    return ModelPrLandingGithubCompleted.model_validate(data)


def test_enforce_result_needs_a_quota_and_a_status_per_request() -> None:
    assert _completed().quota == _QUOTA
    with pytest.raises(ValidationError, match="quota"):
        _completed(quota=None)
    with pytest.raises(ValidationError, match="http_statuses"):
        _completed(http_statuses=())


def test_dry_run_result_records_requests_and_carries_no_response_facts() -> None:
    dry = _completed(mode=EnumPrLandingGithubMode.DRY_RUN, http_statuses=(), quota=None)
    assert dry.requests
    with pytest.raises(ValidationError):
        _completed(
            mode=EnumPrLandingGithubMode.DRY_RUN, http_statuses=(202,), quota=None
        )
    with pytest.raises(ValidationError):
        _completed(mode=EnumPrLandingGithubMode.DRY_RUN, http_statuses=(), quota=_QUOTA)


def test_not_modified_only_on_read_head_checks() -> None:
    with pytest.raises(ValidationError, match="not_modified"):
        _completed(not_modified=True)


def _failed(**kw: Any) -> ModelPrLandingGithubFailed:
    data: dict[str, Any] = {
        **_BASE,
        "operation": EnumPrLandingGithubOperation.UPDATE_BRANCH,
        "reason": EnumPrLandingGithubFailureReason.UPDATE_BRANCH_CONFLICT,
        "detail": "merge conflict between base and head",
        "http_status": 422,
        "retry_after_seconds": None,
        "quota": _QUOTA,
    }
    data.update(kw)
    return ModelPrLandingGithubFailed.model_validate(data)


def test_failed_needs_a_quota_unless_no_response_arrived() -> None:
    assert _failed().reason is EnumPrLandingGithubFailureReason.UPDATE_BRANCH_CONFLICT
    with pytest.raises(ValidationError, match="quota"):
        _failed(quota=None)
    transport = _failed(
        reason=EnumPrLandingGithubFailureReason.TRANSPORT_ERROR,
        http_status=None,
        quota=None,
        detail="connection reset",
    )
    assert transport.quota is None


def test_dry_run_never_fails_against_github() -> None:
    with pytest.raises(ValidationError, match="dry_run"):
        _failed(mode=EnumPrLandingGithubMode.DRY_RUN)


def test_a_quota_floor_refusal_made_no_call() -> None:
    with pytest.raises(ValidationError, match="quota_floor"):
        _failed(reason=EnumPrLandingGithubFailureReason.QUOTA_FLOOR, http_status=429)
