# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""runner_group_get on a recorded fake transport (OMN-24326).

The write node reads one runner group's settings and repositories for the organization and
group id its overlay's runner_group_reads names, so the hourly exposure reading never needs
its own organization permission. The planted control: a group id the overlay does not name
sends nothing (the control fails when the preflight is removed). Organization and group
names here are test-local; the shipped overlay example and the source carry none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
    planned_requests,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_github_repo_write_effect.overlay import (
    ModelPrLabelOverlay,
    ModelRepoWriteOverlay,
    ModelRunnerEntry,
    ModelRunnerGroupEntry,
    ModelRunnerGroupReadOverlay,
    ModelRunnerRemovalOverlay,
)
from omnimarket.nodes.node_github_repo_write_effect.services.runner_registration import (
    get_runner_group_request,
    list_runner_group_repositories_request,
)

pytestmark = pytest.mark.unit

_ORG = "example-org"
_REPO = f"{_ORG}/example-repo"
_GROUP = 7
_KEY = "runner-group-key-0001"
_CORRELATION = UUID("00000000-0000-4000-8000-000000000026")
_NOW = 1_791_000_000.0
_GET = EnumRepoWriteOperation.RUNNER_GROUP_GET

_GROUP_REQUEST = get_runner_group_request(_ORG, _GROUP)
_PAGE_1 = list_runner_group_repositories_request(_ORG, _GROUP, 1)
_PAGE_2 = list_runner_group_repositories_request(_ORG, _GROUP, 2)
_WORKFLOW = "example-org/example-repo/.github/workflows/build.yml@refs/heads/main"


def _headers() -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4900",
        "x-ratelimit-used": "100",
        "x-ratelimit-reset": str(int(_NOW) + 3600),
        "x-ratelimit-resource": "core",
    }


def _ok(body: dict[str, Any]) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=200, headers=_headers(), body=body)


def _group_body(**over: object) -> dict[str, Any]:
    return {
        "id": _GROUP,
        "name": "example-group",
        "allows_public_repositories": False,
        "restricted_to_selected_workflows": True,
        "selected_workflows": [_WORKFLOW],
        **over,
    }


def _repos(count: int, start: int = 0) -> dict[str, Any]:
    return {
        "total_count": count,
        "repositories": [
            {"full_name": f"{_ORG}/example-repo-{start + i}"} for i in range(count)
        ],
    }


@dataclass
class _Fake:
    """A transport that answers a strict, ordered list of exchanges."""

    script: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        assert self.script, f"unexpected request {request.method} {request.path}"
        expected, response = self.script.pop(0)
        assert request == expected
        self.sent.append(request)
        return response


class _NoClaims:
    def active_claim(self, *, claims_dir: str, pr_key: str, now: str) -> None:
        return None


def _overlay(*entries: ModelRunnerGroupEntry) -> ModelRepoWriteOverlay:
    return ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=("lab:ready",)),
        runner_group_reads=(
            ModelRunnerGroupReadOverlay(entries=entries) if entries else None
        ),
    )


def _handler(
    fake: _Fake, overlay: ModelRepoWriteOverlay | None
) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        fake,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE},
        ),
        clock=lambda: _NOW,
        overlay=overlay,
    )


def _request(
    operation: EnumRepoWriteOperation = _GET, **kw: object
) -> ModelRepoWriteRequest:
    fields: dict[str, object] = {
        "correlation_id": _CORRELATION,
        "operation": operation,
        "repo": _REPO,
        "idempotency_key": _KEY,
        "requested_by_lane": "reader-lane",
        **kw,
    }
    if operation is _GET and "runner_group_id" not in kw:
        fields["runner_group_id"] = _GROUP
    return ModelRepoWriteRequest.model_validate(fields)


async def test_a_named_group_is_read_with_its_repositories() -> None:
    fake = _Fake([(_GROUP_REQUEST, _ok(_group_body())), (_PAGE_1, _ok(_repos(2)))])
    result = await _handler(
        fake, _overlay(ModelRunnerGroupEntry(organization=_ORG, group_id=_GROUP))
    ).handle(_request())
    assert isinstance(result, ModelRepoWriteCompleted), result
    assert result.operation is _GET
    assert result.mode is EnumRepoWriteMode.ENFORCE
    group = result.runner_group
    assert group is not None
    assert (group.id, group.name) == (_GROUP, "example-group")
    assert group.allows_public_repositories is False
    assert group.restricted_to_selected_workflows is True
    assert group.selected_workflows == (_WORKFLOW,)
    assert group.repositories == (f"{_ORG}/example-repo-0", f"{_ORG}/example-repo-1")
    assert fake.sent == [_GROUP_REQUEST, _PAGE_1]


async def test_a_full_page_of_repositories_reads_the_next_page() -> None:
    fake = _Fake(
        [
            (_GROUP_REQUEST, _ok(_group_body())),
            (_PAGE_1, _ok(_repos(100))),
            (_PAGE_2, _ok(_repos(1, start=100))),
        ]
    )
    result = await _handler(
        fake,
        _overlay(ModelRunnerGroupEntry(organization=_ORG.upper(), group_id=_GROUP)),
    ).handle(_request())
    assert isinstance(result, ModelRepoWriteCompleted), result
    assert result.runner_group is not None
    assert len(result.runner_group.repositories) == 101
    assert fake.sent == [_GROUP_REQUEST, _PAGE_1, _PAGE_2]


async def test_a_group_the_overlay_does_not_name_sends_nothing() -> None:
    """The failing control: remove the preflight and the transport is called."""
    fake = _Fake([])
    result = await _handler(
        fake, _overlay(ModelRunnerGroupEntry(organization=_ORG, group_id=_GROUP + 1))
    ).handle(_request())
    assert isinstance(result, ModelRepoWriteFailed), result
    assert result.reason is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert str(_GROUP) in result.detail
    assert fake.sent == []


async def test_another_organization_is_refused_before_any_call() -> None:
    fake = _Fake([])
    result = await _handler(
        fake,
        _overlay(
            ModelRunnerGroupEntry(organization="example-other-org", group_id=_GROUP)
        ),
    ).handle(_request())
    assert isinstance(result, ModelRepoWriteFailed), result
    assert result.reason is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert fake.sent == []


async def test_runner_removals_alone_do_not_allow_a_group_read() -> None:
    fake = _Fake([])
    overlay = ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=("lab:ready",)),
        runner_removals=ModelRunnerRemovalOverlay(
            entries=(ModelRunnerEntry(organization=_ORG, name="example-runner-1"),)
        ),
    )
    result = await _handler(fake, overlay).handle(_request())
    assert isinstance(result, ModelRepoWriteFailed), result
    assert result.reason is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert "runner_group_reads" in result.detail
    assert fake.sent == []


async def test_a_body_without_the_exposure_fields_is_refused_not_defaulted() -> None:
    body = {"id": _GROUP, "name": "example-group"}
    fake = _Fake([(_GROUP_REQUEST, _ok(body)), (_PAGE_1, _ok(_repos(0)))])
    result = await _handler(
        fake, _overlay(ModelRunnerGroupEntry(organization=_ORG, group_id=_GROUP))
    ).handle(_request())
    assert isinstance(result, ModelRepoWriteFailed), result
    assert result.reason is EnumRepoWriteRefusal.GITHUB_REFUSED


async def test_dry_run_plans_only_the_group_read() -> None:
    fake = _Fake([])
    result = await _handler(
        fake, _overlay(ModelRunnerGroupEntry(organization=_ORG, group_id=_GROUP))
    ).handle(_request(dry_run=True))
    assert isinstance(result, ModelRepoWriteCompleted), result
    assert result.mode is EnumRepoWriteMode.DRY_RUN
    assert result.requests == (_GROUP_REQUEST,)
    assert fake.sent == []
    assert planned_requests(_request()) == (_GROUP_REQUEST,)


def test_the_group_id_is_required_and_only_this_operation_takes_it() -> None:
    with pytest.raises(ValidationError):
        ModelRepoWriteRequest.model_validate(
            {
                "correlation_id": _CORRELATION,
                "operation": _GET,
                "repo": _REPO,
                "idempotency_key": _KEY,
                "requested_by_lane": "reader-lane",
            }
        )
    with pytest.raises(ValidationError):
        _request(EnumRepoWriteOperation.RUNNER_LIST, runner_group_id=_GROUP)


def test_a_duplicate_overlay_entry_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRunnerGroupReadOverlay(
            entries=(
                ModelRunnerGroupEntry(organization=_ORG, group_id=_GROUP),
                ModelRunnerGroupEntry(organization=_ORG.upper(), group_id=_GROUP),
            )
        )
