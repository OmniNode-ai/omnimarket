# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGithubRepoWriteEffect on a recorded fake transport (OMN-20912).

Every scenario replays a strict, ordered list of exchanges: a request that
differs from the recorded one in method, path or body fails the test, so the
node's request shapes are pinned. Response bodies follow GitHub's documented
REST and GraphQL shapes; a mutation cannot be recorded without mutating.

Covered: each of the six writes; the expected-head refusal; the refused close
(no claim, and a claim held by another lane) against the real claim registry
on a temporary directory; the quota-floor refusal; idempotent replays; and
dry_run, which sends nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    convert_to_draft_request,
    create_issue_comment_request,
    create_pull_request_request,
    edit_pull_request_request,
    git_ref_request,
    issue_comments_request,
    mark_ready_request,
    open_pulls_for_head_request,
    pull_request_request,
    workflow_dispatch_request,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.models.pr_claim import EnumPrClaimOperation, ModelPrClaimRegistryRequest
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
    idempotency_marker,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_pr_claim_lookup import (
    HandlerPrClaimLookup,
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
from omnimarket.nodes.node_github_repo_write_effect.protocols import (
    ProtocolPrClaimLookup,
)
from omnimarket.nodes.node_pr_claim_registry_effect.handlers.handler_pr_claim_registry import (
    HandlerPrClaimRegistry,
)

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnimarket"
_HEAD = "4ceedeafeb4040c987883d96c20e745ad5eed6e1"
_MOVED = "a7f20d7a0" + "0" * 31
_NODE_ID = "PR_kwDOR6jjtc8AAAABFQhbqA"
_CORRELATION = UUID("00000000-0000-4000-8000-000000020912")
_LANE = "scm-ops-5d21"
_RUN = "run-omn20912"
_NOW = 1_791_000_000.0  # the handler's clock; every recorded reset is an hour later
_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_github_repo_write_effect"
    / "contract.yaml"
)


def _headers(remaining: int = 4_900, resource: str = "core") -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-used": str(5000 - remaining),
        "x-ratelimit-reset": str(int(_NOW) + 3600),
        "x-ratelimit-resource": resource,
    }


def _ok(
    body: dict[str, object] | list[object] | None,
    *,
    status: int = 200,
    remaining: int = 4_900,
    resource: str = "core",
) -> ModelGithubHttpResponse:
    wrapped = {"value": body} if isinstance(body, list) else body
    return ModelGithubHttpResponse(
        status=status, headers=_headers(remaining, resource), body=wrapped
    )


def _pr(
    *, draft: bool = False, state: str = "open", head: str = _HEAD
) -> dict[str, object]:
    return {
        "number": 4000,
        "node_id": _NODE_ID,
        "state": state,
        "title": "old title",
        "body": "old body",
        "draft": draft,
        "html_url": f"https://github.com/{_REPO}/pull/4000",
        "head": {"ref": "omn-20912-scratch", "sha": head},
        "base": {"ref": "dev"},
    }


@dataclass
class _Recorded:
    """Strict ordered replay of (request, response) pairs."""

    exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        assert self.exchanges, f"no recorded exchange left for {request!r}"
        expected, response = self.exchanges.pop(0)
        assert request == expected, f"sent {request!r}, recorded {expected!r}"
        return response

    def drained(self) -> bool:
        return not self.exchanges


class _NoClaims:
    def active_claim(self, *, claims_dir: str, pr_key: str, now: str) -> None:
        return None


def _handler(
    transport: _Recorded,
    *,
    claims: ProtocolPrClaimLookup | None = None,
    enforce: bool = True,
) -> HandlerGithubRepoWriteEffect:
    config = ModelRepoWriteConfig(
        github_mode=EnumRepoWriteMode.DRY_RUN,
        github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE} if enforce else {},
    )
    return HandlerGithubRepoWriteEffect(
        transport,
        claim_lookup=claims or _NoClaims(),
        config=config,
        clock=lambda: _NOW,
    )


def _req(op: EnumRepoWriteOperation, **kw: object) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": op,
            "repo": _REPO,
            "idempotency_key": f"omn20912-{op.value}",
            "requested_by_lane": _LANE,
            **kw,
        }
    )


# --- pr_create ----------------------------------------------------------------


async def test_pr_create_posts_when_no_open_pr_exists_for_the_head() -> None:
    create = create_pull_request_request(
        _REPO, title="t", head="omn-20912-scratch", base="dev", body="b", draft=True
    )
    transport = _Recorded(
        [
            (open_pulls_for_head_request(_REPO, "omn-20912-scratch"), _ok([])),
            (create, _ok(_pr(draft=True), status=201)),
        ]
    )
    result = await _handler(transport).handle(
        _req(
            EnumRepoWriteOperation.PR_CREATE,
            title="t",
            head="omn-20912-scratch",
            base="dev",
            body="b",
            draft=True,
        )
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.pr_number, result.replayed, result.mode) == (
        4000,
        False,
        EnumRepoWriteMode.ENFORCE,
    )
    assert result.quota is not None
    assert result.quota.remaining == 4_900
    assert transport.drained()


async def test_pr_create_returns_the_open_pr_for_the_head_without_creating() -> None:
    transport = _Recorded(
        [(open_pulls_for_head_request(_REPO, "omn-20912-scratch"), _ok([_pr()]))]
    )
    result = await _handler(transport).handle(
        _req(
            EnumRepoWriteOperation.PR_CREATE,
            title="t",
            head="omn-20912-scratch",
            base="dev",
        )
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is True
    assert result.pr_number == 4000
    assert all(r.method == "GET" for r in transport.sent)


async def test_same_idempotency_key_twice_sends_nothing_the_second_time() -> None:
    create = create_pull_request_request(
        _REPO, title="t", head="h1", base="dev", body="", draft=False
    )
    transport = _Recorded(
        [
            (open_pulls_for_head_request(_REPO, "h1"), _ok([])),
            (create, _ok(_pr(), status=201)),
        ]
    )
    handler = _handler(transport)
    request = _req(EnumRepoWriteOperation.PR_CREATE, title="t", head="h1", base="dev")
    first = await handler.handle(request)
    second = await handler.handle(request)
    assert isinstance(first, ModelRepoWriteCompleted)
    assert isinstance(second, ModelRepoWriteCompleted)
    assert (first.replayed, second.replayed) == (False, True)
    assert second.pr_number == first.pr_number
    assert len(transport.sent) == 2


async def test_pr_create_refuses_a_moved_branch_tip() -> None:
    transport = _Recorded(
        [(git_ref_request(_REPO, "h1"), _ok({"object": {"sha": _MOVED}}))]
    )
    result = await _handler(transport).handle(
        _req(
            EnumRepoWriteOperation.PR_CREATE,
            title="t",
            head="h1",
            base="dev",
            expected_head_sha=_HEAD,
        )
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.HEAD_MOVED


async def test_pr_create_github_refusal_is_typed() -> None:
    create = create_pull_request_request(
        _REPO, title="t", head="h2", base="dev", body="", draft=False
    )
    transport = _Recorded(
        [
            (open_pulls_for_head_request(_REPO, "h2"), _ok([])),
            (create, _ok({"message": "Validation Failed"}, status=422)),
        ]
    )
    result = await _handler(transport).handle(
        _req(EnumRepoWriteOperation.PR_CREATE, title="t", head="h2", base="dev")
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert (result.reason, result.http_status) == (
        EnumRepoWriteRefusal.GITHUB_REFUSED,
        422,
    )
    assert "Validation Failed" in result.detail


# --- pr_edit and pr_ready -----------------------------------------------------


async def test_pr_edit_patches_changed_fields_and_converts_to_draft() -> None:
    transport = _Recorded(
        [
            (pull_request_request(_REPO, 4000), _ok(_pr())),
            (edit_pull_request_request(_REPO, 4000, {"title": "new"}), _ok(_pr())),
            (
                convert_to_draft_request(_NODE_ID),
                _ok({"data": {"convertPullRequestToDraft": {}}}, resource="graphql"),
            ),
        ]
    )
    result = await _handler(transport).handle(
        _req(
            EnumRepoWriteOperation.PR_EDIT,
            pr_number=4000,
            expected_head_sha=_HEAD,
            title="new",
            body="old body",
            draft=True,
        )
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is False
    assert transport.drained()


async def test_pr_edit_refuses_a_moved_head_with_no_mutation() -> None:
    transport = _Recorded([(pull_request_request(_REPO, 4000), _ok(_pr(head=_MOVED)))])
    result = await _handler(transport).handle(
        _req(
            EnumRepoWriteOperation.PR_EDIT,
            pr_number=4000,
            expected_head_sha=_HEAD,
            title="new",
        )
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.HEAD_MOVED
    assert [r.method for r in transport.sent] == ["GET"]


async def test_pr_ready_marks_a_draft_ready() -> None:
    transport = _Recorded(
        [
            (pull_request_request(_REPO, 4000), _ok(_pr(draft=True))),
            (
                mark_ready_request(_NODE_ID),
                _ok(
                    {"data": {"markPullRequestReadyForReview": {}}}, resource="graphql"
                ),
            ),
        ]
    )
    result = await _handler(transport).handle(
        _req(EnumRepoWriteOperation.PR_READY, pr_number=4000, expected_head_sha=_HEAD)
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is False
    assert result.head_sha == _HEAD


async def test_pr_ready_on_a_ready_pr_is_a_replay() -> None:
    transport = _Recorded([(pull_request_request(_REPO, 4000), _ok(_pr()))])
    result = await _handler(transport).handle(
        _req(EnumRepoWriteOperation.PR_READY, pr_number=4000, expected_head_sha=_HEAD)
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is True


async def test_graphql_errors_in_a_200_are_a_refusal() -> None:
    transport = _Recorded(
        [
            (pull_request_request(_REPO, 4000), _ok(_pr(draft=True))),
            (
                mark_ready_request(_NODE_ID),
                _ok({"errors": [{"message": "not allowed"}]}, resource="graphql"),
            ),
        ]
    )
    result = await _handler(transport).handle(
        _req(EnumRepoWriteOperation.PR_READY, pr_number=4000, expected_head_sha=_HEAD)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.GITHUB_REFUSED
    assert "not allowed" in result.detail


# --- pr_close and the claim ----------------------------------------------------


def _acquire(claims_dir: Path, *, lane: str, run: str) -> None:
    registry = HandlerPrClaimRegistry()
    result = registry.handle(
        ModelPrClaimRegistryRequest(
            operation=EnumPrClaimOperation.ACQUIRE,
            claims_dir=str(claims_dir),
            now=datetime.fromtimestamp(_NOW - 60, tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            instance_id_path=str(claims_dir / "instance-id"),
            host="lab-host",
            pr_key=f"{_REPO.lower()}#4000",
            run_id=run,
            action="close",
            lane_id=lane,
        )
    )
    assert result.succeeded


def _close(claims_dir: Path, **kw: object) -> ModelRepoWriteRequest:
    return _req(
        EnumRepoWriteOperation.PR_CLOSE,
        pr_number=4000,
        expected_head_sha=_HEAD,
        claims_dir=str(claims_dir),
        requested_by_run=_RUN,
        **kw,
    )


async def test_pr_close_without_a_claim_is_refused_before_any_call(
    tmp_path: Path,
) -> None:
    transport = _Recorded([])
    result = await _handler(transport, claims=HandlerPrClaimLookup()).handle(
        _close(tmp_path)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.CLAIM_NOT_HELD
    assert transport.sent == []


async def test_pr_close_with_another_lanes_claim_is_refused(tmp_path: Path) -> None:
    _acquire(tmp_path, lane="some-other-lane", run=_RUN)
    transport = _Recorded([])
    result = await _handler(transport, claims=HandlerPrClaimLookup()).handle(
        _close(tmp_path)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.CLAIM_NOT_HELD
    assert "some-other-lane" in result.detail
    assert transport.sent == []


async def test_pr_close_with_the_lanes_claim_closes_the_pr(tmp_path: Path) -> None:
    _acquire(tmp_path, lane=_LANE, run=_RUN)
    transport = _Recorded(
        [
            (pull_request_request(_REPO, 4000), _ok(_pr())),
            (
                edit_pull_request_request(_REPO, 4000, {"state": "closed"}),
                _ok(_pr(state="closed")),
            ),
        ]
    )
    result = await _handler(transport, claims=HandlerPrClaimLookup()).handle(
        _close(tmp_path)
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.replayed is False
    assert transport.drained()


async def test_quota_floor_refuses_the_next_call_before_it_is_sent(
    tmp_path: Path,
) -> None:
    _acquire(tmp_path, lane=_LANE, run=_RUN)
    transport = _Recorded(
        [(pull_request_request(_REPO, 4000), _ok(_pr(), remaining=120))]
    )
    result = await _handler(transport, claims=HandlerPrClaimLookup()).handle(
        _close(tmp_path)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.QUOTA_FLOOR
    assert result.quota is not None
    assert result.quota.remaining == 120
    # the read went out; the close never did
    assert [r.method for r in transport.sent] == ["GET"]


# --- pr_comment ---------------------------------------------------------------


async def test_pr_comment_posts_with_its_idempotency_marker() -> None:
    marker = idempotency_marker("omn20912-pr_comment")
    transport = _Recorded(
        [
            (
                issue_comments_request(_REPO, 4000, per_page=LIST_PAGE_SIZE),
                _ok([{"id": 1, "body": "unrelated"}]),
            ),
            (
                create_issue_comment_request(_REPO, 4000, f"hello\n\n{marker}"),
                _ok({"id": 77, "html_url": "https://github.com/x#c77"}, status=201),
            ),
        ]
    )
    result = await _handler(transport).handle(
        _req(EnumRepoWriteOperation.PR_COMMENT, pr_number=4000, body="hello")
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.comment_id, result.replayed) == (77, False)


async def test_pr_comment_found_by_its_marker_is_not_posted_again() -> None:
    marker = idempotency_marker("omn20912-pr_comment")
    transport = _Recorded(
        [
            (
                issue_comments_request(_REPO, 4000, per_page=LIST_PAGE_SIZE),
                _ok([{"id": 77, "body": f"hello\n\n{marker}"}]),
            ),
        ]
    )
    result = await _handler(transport).handle(
        _req(EnumRepoWriteOperation.PR_COMMENT, pr_number=4000, body="hello")
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert (result.comment_id, result.replayed) == (77, True)


# --- workflow_dispatch --------------------------------------------------------


async def test_workflow_dispatch_sends_the_dispatch() -> None:
    dispatch = workflow_dispatch_request(_REPO, "ci.yml", "dev", {"reason": "lab"})
    transport = _Recorded([(dispatch, _ok(None, status=204))])
    result = await _handler(transport).handle(
        _req(
            EnumRepoWriteOperation.WORKFLOW_DISPATCH,
            workflow="ci.yml",
            ref="dev",
            inputs={"reason": "lab"},
        )
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.http_statuses == (204,)


async def test_workflow_dispatch_refuses_a_moved_ref() -> None:
    transport = _Recorded(
        [(git_ref_request(_REPO, "dev"), _ok({"object": {"sha": _MOVED}}))]
    )
    result = await _handler(transport).handle(
        _req(
            EnumRepoWriteOperation.WORKFLOW_DISPATCH,
            workflow="ci.yml",
            ref="dev",
            expected_head_sha=_HEAD,
        )
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.HEAD_MOVED


# --- dry_run ------------------------------------------------------------------


async def test_a_repository_not_in_the_enforce_table_runs_dry() -> None:
    transport = _Recorded([])
    result = await _handler(transport, enforce=False).handle(
        _req(EnumRepoWriteOperation.PR_READY, pr_number=4000, expected_head_sha=_HEAD)
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.mode is EnumRepoWriteMode.DRY_RUN
    assert [r.method for r in result.requests] == ["GET", "POST"]
    assert transport.sent == []


async def test_a_request_can_force_dry_run_but_not_enforce() -> None:
    transport = _Recorded([])
    result = await _handler(transport).handle(
        _req(
            EnumRepoWriteOperation.WORKFLOW_DISPATCH,
            workflow="ci.yml",
            ref="dev",
            dry_run=True,
        )
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.mode is EnumRepoWriteMode.DRY_RUN
    assert transport.sent == []


async def test_dry_run_close_still_needs_the_claim(tmp_path: Path) -> None:
    transport = _Recorded([])
    result = await _handler(transport, claims=HandlerPrClaimLookup()).handle(
        _close(tmp_path, dry_run=True)
    )
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.CLAIM_NOT_HELD


# --- request validation and the contract ---------------------------------------


@pytest.mark.parametrize(
    ("op", "kw", "missing"),
    [
        (EnumRepoWriteOperation.PR_READY, {"pr_number": 1}, "expected_head_sha"),
        (
            EnumRepoWriteOperation.PR_CLOSE,
            {"pr_number": 1, "expected_head_sha": _HEAD},
            "claims_dir",
        ),
        (EnumRepoWriteOperation.PR_CREATE, {"head": "h"}, "base"),
        (EnumRepoWriteOperation.WORKFLOW_DISPATCH, {"workflow": "ci.yml"}, "ref"),
        (EnumRepoWriteOperation.PR_COMMENT, {"pr_number": 1}, "body"),
    ],
)
def test_each_operation_requires_its_fields(
    op: EnumRepoWriteOperation, kw: dict[str, object], missing: str
) -> None:
    with pytest.raises(ValueError, match=missing):
        _req(op, **kw)


def test_contract_declares_mode_floor_and_topics() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    config = ModelRepoWriteConfig.from_contract(_CONTRACT)
    assert config.github_mode is EnumRepoWriteMode.DRY_RUN
    assert config.mode_for("omninode-ai/OMNIMARKET") is EnumRepoWriteMode.ENFORCE
    assert config.mode_for("OmniNode-ai/omnibase_core") is EnumRepoWriteMode.DRY_RUN
    assert contract["quota_floor"]["min_remaining"] == {"core": 500, "graphql": 500}
    assert {op["name"] for op in contract["operations"]} == {
        op.value for op in EnumRepoWriteOperation
    }
    routed = {
        f"Model{e['event_type']}": e["topic"] for e in contract["published_events"]
    }
    assert routed == {
        ModelRepoWriteCompleted.__name__: "onex.evt.omnimarket.github-repo-write-completed.v1",
        ModelRepoWriteFailed.__name__: "onex.evt.omnimarket.github-repo-write-failed.v1",
    }
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.github-repo-write-requested.v1"
    ]
