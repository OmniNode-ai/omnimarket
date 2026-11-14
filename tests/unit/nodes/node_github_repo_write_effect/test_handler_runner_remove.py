# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""runner_list and runner_remove on a recorded fake transport (OMN-24274).

Each scenario replays a strict, ordered list of exchanges, so the request shapes
are pinned. The planted controls prove the refusals: a runner name outside the
overlay's runner_removals sends nothing (the control fails when the allowlist
check is removed), an online or busy runner is never deleted, and a runner
GitHub does not register is a no-op. The organization and runner names here are
test-local; the shipped overlay example and the node's source carry none.
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
    ModelRunnerRemovalOverlay,
)
from omnimarket.nodes.node_github_repo_write_effect.services.runner_registration import (
    delete_runner_request,
    list_runners_request,
)

pytestmark = pytest.mark.unit

_ORG = "example-org"
_REPO = f"{_ORG}/example-repo"
_OTHER_ORG_REPO = "example-other-org/example-repo"
_KEY = "runner-key-0001"
_CORRELATION = UUID("00000000-0000-4000-8000-000000000024")
_NOW = 1_791_000_000.0
_READY = "lab:ready"

_LIST = EnumRepoWriteOperation.RUNNER_LIST
_REMOVE = EnumRepoWriteOperation.RUNNER_REMOVE
_ONE = "example-runner-1"
_TWO = "example-runner-2"
_OFF_LIMITS = "example-runner-off-limits"

_PAGE_1 = list_runners_request(_ORG, 1)
_PAGE_2 = list_runners_request(_ORG, 2)


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


def _no_content() -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=204, headers=_headers(), body=None)


def _runner(
    runner_id: int, name: str, status: str = "offline", busy: bool = False
) -> dict[str, Any]:
    return {"id": runner_id, "name": name, "status": status, "busy": busy}


def _runners(*runners: dict[str, Any]) -> ModelGithubHttpResponse:
    return _ok({"total_count": len(runners), "runners": list(runners)})


@dataclass
class _Recorded:
    exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        assert self.exchanges, f"no recorded exchange left for {request!r}"
        expected, response = self.exchanges.pop(0)
        assert request == expected, f"sent {request!r}, recorded {expected!r}"
        return response


class _NoClaims:
    def active_claim(self, *, claims_dir: str, pr_key: str, now: str) -> None:
        return None


def _overlay(
    entries: tuple[ModelRunnerEntry, ...] | None = (
        ModelRunnerEntry(organization=_ORG, name=_ONE),
        ModelRunnerEntry(organization=_ORG, name=_TWO),
    ),
) -> ModelRepoWriteOverlay:
    return ModelRepoWriteOverlay(
        pr_label=ModelPrLabelOverlay(allowed_labels=(_READY,)),
        runner_removals=(
            ModelRunnerRemovalOverlay(entries=entries) if entries is not None else None
        ),
    )


def _handler(
    transport: _Recorded, overlay: ModelRepoWriteOverlay | None = None
) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=_NoClaims(),
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository={
                _REPO: EnumRepoWriteMode.ENFORCE,
                _OTHER_ORG_REPO: EnumRepoWriteMode.ENFORCE,
            },
        ),
        clock=lambda: _NOW,
        overlay=overlay if overlay is not None else _overlay(),
    )


def _request(
    operation: EnumRepoWriteOperation, repo: str = _REPO, **kw: object
) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": operation,
            "repo": repo,
            "idempotency_key": _KEY,
            "requested_by_lane": "author-lane",
            **kw,
        }
    )


# --- list ---------------------------------------------------------------------


async def test_runner_list_pages_until_a_short_page_and_answers_every_runner() -> None:
    first_page = [_runner(i, f"example-bulk-{i}") for i in range(1, 101)]
    transport = _Recorded(
        [
            (_PAGE_1, _runners(*first_page)),
            (_PAGE_2, _runners(_runner(501, _ONE, status="online", busy=True))),
        ]
    )
    result = await _handler(transport).handle(_request(_LIST))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.mode, result.runner_removed) == (EnumRepoWriteMode.ENFORCE, None)
    assert result.runners is not None
    assert [r.name for r in result.runners][-1] == _ONE
    assert len(result.runners) == 101
    assert (result.runners[-1].status, result.runners[-1].busy) == ("online", True)
    assert transport.sent == [_PAGE_1, _PAGE_2]
    assert _PAGE_1.method == "GET"
    assert _PAGE_1.path == f"/orgs/{_ORG}/actions/runners?per_page=100&page=1"
    assert not transport.exchanges


async def test_runner_list_of_an_organization_the_overlay_does_not_name_is_refused() -> (
    None
):
    transport = _Recorded([])
    result = await _handler(transport).handle(_request(_LIST, repo=_OTHER_ORG_REPO))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert transport.sent == []


# --- remove -------------------------------------------------------------------


async def test_an_offline_idle_runner_is_deleted_by_its_id() -> None:
    delete = delete_runner_request(_ORG, 42)
    transport = _Recorded(
        [
            (_PAGE_1, _runners(_runner(41, _TWO), _runner(42, _ONE))),
            (delete, _no_content()),
        ]
    )
    result = await _handler(transport).handle(_request(_REMOVE, runner_name=_ONE))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.replayed, result.mode) == (False, EnumRepoWriteMode.ENFORCE)
    assert (result.runner_name, result.runner_removed) == (_ONE, True)
    assert transport.sent == [_PAGE_1, delete]
    assert delete.method == "DELETE"
    assert delete.path == f"/orgs/{_ORG}/actions/runners/42"
    assert not transport.exchanges


async def test_a_runner_github_does_not_register_is_a_no_op_with_no_delete() -> None:
    transport = _Recorded([(_PAGE_1, _runners(_runner(41, _TWO)))])
    result = await _handler(transport).handle(_request(_REMOVE, runner_name=_ONE))
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is True
    assert (result.runner_name, result.runner_removed) == (_ONE, False)
    assert transport.sent == [_PAGE_1]


@pytest.mark.parametrize(
    ("status", "busy"), [("online", False), ("offline", True), ("online", True)]
)
async def test_an_online_or_busy_runner_is_refused_and_never_deleted(
    status: str, busy: bool
) -> None:
    transport = _Recorded(
        [(_PAGE_1, _runners(_runner(42, _ONE, status=status, busy=busy)))]
    )
    result = await _handler(transport).handle(_request(_REMOVE, runner_name=_ONE))
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.RUNNER_ONLINE
    assert all(sent.method != "DELETE" for sent in transport.sent)
    assert transport.sent == [_PAGE_1]


async def test_a_runner_name_outside_the_overlay_is_refused_before_any_call() -> None:
    # The planted control: this fails when the allowlist check is removed,
    # because the offline runner below would then be found and deleted.
    transport = _Recorded(
        [
            (_PAGE_1, _runners(_runner(43, _OFF_LIMITS))),
            (delete_runner_request(_ORG, 43), _no_content()),
        ]
    )
    result = await _handler(transport).handle(
        _request(_REMOVE, runner_name=_OFF_LIMITS)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert result.terminal_failure_cause is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert transport.sent == []


async def test_an_allowed_name_on_another_organization_is_refused() -> None:
    transport = _Recorded([])
    result = await _handler(transport).handle(
        _request(_REMOVE, repo=_OTHER_ORG_REPO, runner_name=_ONE)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert transport.sent == []


@pytest.mark.parametrize("operation", [_LIST, _REMOVE])
async def test_an_overlay_without_runner_removals_refuses_both_operations(
    operation: EnumRepoWriteOperation,
) -> None:
    transport = _Recorded([])
    extra = {"runner_name": _ONE} if operation is _REMOVE else {}
    result = await _handler(transport, _overlay(entries=None)).handle(
        _request(operation, **extra)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.RUNNER_NOT_ALLOWED
    assert "runner_removals" in result.detail
    assert transport.sent == []


async def test_a_dry_run_plans_the_one_read_and_sends_nothing() -> None:
    transport = _Recorded([])
    request = _request(_REMOVE, runner_name=_ONE, dry_run=True)
    result = await _handler(transport).handle(request)
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.mode is EnumRepoWriteMode.DRY_RUN
    assert result.requests == (_PAGE_1,)
    assert planned_requests(request) == (_PAGE_1,)
    assert transport.sent == []


async def test_a_repeated_remove_reads_github_again_and_keeps_no_record() -> None:
    delete = delete_runner_request(_ORG, 42)
    transport = _Recorded(
        [
            (_PAGE_1, _runners(_runner(42, _ONE))),
            (delete, _no_content()),
            (_PAGE_1, _runners()),
        ]
    )
    handler = _handler(transport)
    first = await handler.handle(_request(_REMOVE, runner_name=_ONE))
    second = await handler.handle(_request(_REMOVE, runner_name=_ONE))
    assert isinstance(first, ModelRepoWriteCompleted)
    assert isinstance(second, ModelRepoWriteCompleted)
    assert (first.runner_removed, first.replayed) == (True, False)
    assert (second.runner_removed, second.replayed) == (False, True)
    assert not transport.exchanges


# --- request shape ------------------------------------------------------------


def test_runner_remove_requires_a_runner_name() -> None:
    with pytest.raises(ValidationError, match="runner_name"):
        _request(_REMOVE)


@pytest.mark.parametrize("operation", [_LIST, EnumRepoWriteOperation.PR_READY])
def test_runner_name_is_read_only_by_runner_remove(
    operation: EnumRepoWriteOperation,
) -> None:
    extra: dict[str, object] = {"pr_number": 7} if operation.value == "pr_ready" else {}
    with pytest.raises(ValidationError, match="runner_name"):
        _request(operation, runner_name=_ONE, **extra)


def test_a_runner_name_is_never_in_another_operations_envelope() -> None:
    dumped = _request(_LIST).model_dump(mode="json")
    assert "runner_name" not in dumped
    assert (
        _request(_REMOVE, runner_name=_ONE).model_dump(mode="json")["runner_name"]
        == _ONE
    )


def test_the_overlay_refuses_a_runner_named_twice() -> None:
    with pytest.raises(ValidationError, match="named twice"):
        ModelRunnerRemovalOverlay(
            entries=(
                ModelRunnerEntry(organization=_ORG, name=_ONE),
                ModelRunnerEntry(organization=_ORG.upper(), name=_ONE),
            )
        )
