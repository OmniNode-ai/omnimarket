# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GitHub write effect refuses stale, expired and unreadable fences (OMN-21425).

A command acting on a leased resource carries ``lease: {kind, resource, fence,
expires_at}``. Before any GitHub call the effect asks its fence port whether the
fence still names the resource's live grant (omnibase_infra's ``check_fence``,
OMN-21424) and refuses every answer but ``ok``: ``stale`` is
``lease_fence_stale``, ``expired`` is ``lease_expired``, ``unknown`` (or no
port, or a port that raises) is ``lease_unreadable``. A lease that expires
before the effect's own deadline (the contract's ``descriptor.timeout_ms``) is
refused ``lease_expired`` without asking. This is the GenericLease TLA+
model's NoEffectWithStaleFence invariant on the effect side; a verb that skips
the check is its mut_effect_skips_check mutation, so every verb is pinned here.

The transport records every call: a refusal that sent anything fails the test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    create_issue_comment_request,
    issue_comments_request,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
    idempotency_marker,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteFenceStatus,
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteLease,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_github_repo_write_effect.protocols import (
    ProtocolLeaseFence,
)

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnibase_spi"
_HEAD = "4ceedeafeb4040c987883d96c20e745ad5eed6e1"
_CORRELATION = UUID("00000000-0000-4000-8000-000000021425")
_LANE = "fence-21425-0440"
_RUN = "run-omn21425"
_NOW = 1_791_000_000.0
_KIND = "landing-fix"
_RESOURCE = f"pr:{_REPO}#4000"
_LIVE_FENCE = 12


def _headers() -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "4900",
        "x-ratelimit-used": "100",
        "x-ratelimit-reset": str(int(_NOW) + 3600),
        "x-ratelimit-resource": "core",
    }


class _RecordingTransport:
    """Records every request; answers only the exchanges it was given."""

    def __init__(
        self,
        exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
        | None = None,
    ) -> None:
        self.exchanges = list(exchanges or [])
        self.sent: list[ModelGithubHttpRequest] = []

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        assert self.exchanges, f"GitHub call recorded: {request!r}"
        expected, response = self.exchanges.pop(0)
        assert request == expected, f"sent {request!r}, recorded {expected!r}"
        return response


class _LiveFencePort:
    """The check_fence contract over a table of live grants.

    ``ok`` for the live fence, ``stale`` below it, ``expired`` with no live
    grant; ``unknown`` above it, as omnibase_infra's check_fence answers.
    """

    def __init__(self, live: dict[tuple[str, str], int]) -> None:
        self.live = live
        self.calls: list[tuple[str, str, int]] = []

    async def check_fence(self, *, kind: str, resource: str, fence: int) -> str:
        self.calls.append((kind, resource, fence))
        held = self.live.get((kind, resource))
        if held is None:
            return EnumRepoWriteFenceStatus.EXPIRED
        if fence == held:
            return EnumRepoWriteFenceStatus.OK
        if fence < held:
            return EnumRepoWriteFenceStatus.STALE
        return EnumRepoWriteFenceStatus.UNKNOWN


class _FixedPort:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0

    async def check_fence(self, *, kind: str, resource: str, fence: int) -> str:
        self.calls += 1
        return self.answer


class _RaisingPort:
    async def check_fence(self, *, kind: str, resource: str, fence: int) -> str:
        raise ConnectionError("etcd unreachable")


def _handler(
    transport: _RecordingTransport,
    port: ProtocolLeaseFence | None,
    *,
    deadline_seconds: float | None = 30.0,
) -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        transport,
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE},
        ),
        runtime_claims_dir="",
        clock=lambda: _NOW,
        fence_port=port,
        deadline_seconds=deadline_seconds,
    )


def _lease(*, fence: int = _LIVE_FENCE, ttl_s: float = 600.0) -> ModelRepoWriteLease:
    return ModelRepoWriteLease(
        kind=_KIND,
        resource=_RESOURCE,
        fence=fence,
        expires_at=datetime.fromtimestamp(_NOW + ttl_s, tz=UTC),
    )


#: One valid command per write verb the effect serves.
_VERB_FIELDS: dict[EnumRepoWriteOperation, dict[str, object]] = {
    EnumRepoWriteOperation.PR_COMMENT: {"pr_number": 4000, "body": "hello"},
    EnumRepoWriteOperation.PR_EDIT: {
        "pr_number": 4000,
        "expected_head_sha": _HEAD,
        "title": "new title",
    },
    EnumRepoWriteOperation.PR_READY: {"pr_number": 4000, "expected_head_sha": _HEAD},
    EnumRepoWriteOperation.PR_CLOSE: {
        "pr_number": 4000,
        "expected_head_sha": _HEAD,
        "requested_by_run": _RUN,
    },
    EnumRepoWriteOperation.PR_CREATE: {
        "head": "omn-21425-scratch",
        "base": "dev",
        "title": "OMN-21425 t",
    },
    EnumRepoWriteOperation.PR_THREAD_REPLY: {
        "pr_number": 4000,
        "thread_comment_id": 77,
        "body": "reply",
    },
    EnumRepoWriteOperation.WORKFLOW_DISPATCH: {"workflow": "ci.yml", "ref": "dev"},
    EnumRepoWriteOperation.CHECK_RUN_CREATE: {
        "name": "lab-pass",
        "head_sha": _HEAD,
        "status": "queued",
    },
}


def _req(
    op: EnumRepoWriteOperation,
    lease: ModelRepoWriteLease | None,
    **kw: object,
) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": _CORRELATION,
            "operation": op,
            "repo": _REPO,
            "idempotency_key": f"omn21425-{op.value}",
            "requested_by_lane": _LANE,
            "lease": lease,
            **_VERB_FIELDS[op],
            **kw,
        }
    )


def _assert_refused(
    result: object,
    transport: _RecordingTransport,
    reason: EnumRepoWriteRefusal,
) -> None:
    assert transport.sent == [], "a refused write made a GitHub call"
    assert isinstance(result, ModelRepoWriteFailed), result
    assert result.reason is reason, (result.reason, result.detail)
    assert result.terminal_failure_cause is reason
    assert result.requests == ()


def test_every_operation_is_covered_here() -> None:
    assert set(_VERB_FIELDS) == set(EnumRepoWriteOperation)


# --- AC1: a fence lower than the live one is refused lease_fence_stale ----------


@pytest.mark.parametrize("op", list(_VERB_FIELDS), ids=lambda op: op.value)
async def test_a_stale_fence_is_refused_on_every_verb(
    op: EnumRepoWriteOperation,
) -> None:
    transport = _RecordingTransport()
    port = _LiveFencePort({(_KIND, _RESOURCE): _LIVE_FENCE})
    result = await _handler(transport, port).handle(
        _req(op, _lease(fence=_LIVE_FENCE - 1))
    )
    _assert_refused(result, transport, EnumRepoWriteRefusal.LEASE_FENCE_STALE)
    assert port.calls == [(_KIND, _RESOURCE, _LIVE_FENCE - 1)]
    assert "stale" in result.detail


async def test_a_stale_fence_is_refused_in_dry_run_too() -> None:
    transport = _RecordingTransport()
    port = _LiveFencePort({(_KIND, _RESOURCE): _LIVE_FENCE})
    result = await _handler(transport, port).handle(
        _req(EnumRepoWriteOperation.PR_COMMENT, _lease(fence=3), dry_run=True)
    )
    _assert_refused(result, transport, EnumRepoWriteRefusal.LEASE_FENCE_STALE)
    assert result.mode is EnumRepoWriteMode.DRY_RUN


async def test_the_live_fence_writes() -> None:
    marker = idempotency_marker("omn21425-pr_comment")
    created = {"id": 9, "html_url": f"https://github.com/{_REPO}/pull/4000#c9"}
    transport = _RecordingTransport(
        [
            (
                issue_comments_request(_REPO, 4000, per_page=LIST_PAGE_SIZE),
                ModelGithubHttpResponse(
                    status=200, headers=_headers(), body={"value": []}
                ),
            ),
            (
                create_issue_comment_request(_REPO, 4000, f"hello\n\n{marker}"),
                ModelGithubHttpResponse(status=201, headers=_headers(), body=created),
            ),
        ]
    )
    port = _LiveFencePort({(_KIND, _RESOURCE): _LIVE_FENCE})
    result = await _handler(transport, port).handle(
        _req(EnumRepoWriteOperation.PR_COMMENT, _lease())
    )
    assert isinstance(result, ModelRepoWriteCompleted), result
    assert result.comment_id == 9
    assert port.calls == [(_KIND, _RESOURCE, _LIVE_FENCE)]
    assert transport.exchanges == []


async def test_a_command_without_a_lease_is_not_fenced() -> None:
    port = _FixedPort(EnumRepoWriteFenceStatus.STALE)
    result = await _handler(_RecordingTransport(), port).handle(
        _req(EnumRepoWriteOperation.PR_COMMENT, None, dry_run=True)
    )
    assert isinstance(result, ModelRepoWriteCompleted)
    assert port.calls == 0


# --- AC2: a lease that expires before the effect's deadline ---------------------


@pytest.mark.parametrize("op", list(_VERB_FIELDS), ids=lambda op: op.value)
async def test_a_lease_expiring_inside_the_deadline_is_refused_before_any_call(
    op: EnumRepoWriteOperation,
) -> None:
    transport = _RecordingTransport()
    port = _LiveFencePort({(_KIND, _RESOURCE): _LIVE_FENCE})
    result = await _handler(transport, port, deadline_seconds=30.0).handle(
        _req(op, _lease(ttl_s=10.0))
    )
    _assert_refused(result, transport, EnumRepoWriteRefusal.LEASE_EXPIRED)


async def test_an_already_expired_lease_is_refused() -> None:
    transport = _RecordingTransport()
    port = _LiveFencePort({(_KIND, _RESOURCE): _LIVE_FENCE})
    result = await _handler(transport, port).handle(
        _req(EnumRepoWriteOperation.PR_READY, _lease(ttl_s=-5.0))
    )
    _assert_refused(result, transport, EnumRepoWriteRefusal.LEASE_EXPIRED)


async def test_the_port_reading_expired_is_refused_lease_expired() -> None:
    transport = _RecordingTransport()
    port = _LiveFencePort({})  # no live grant: the key is gone
    result = await _handler(transport, port).handle(
        _req(EnumRepoWriteOperation.PR_EDIT, _lease())
    )
    _assert_refused(result, transport, EnumRepoWriteRefusal.LEASE_EXPIRED)
    assert port.calls == [(_KIND, _RESOURCE, _LIVE_FENCE)]


async def test_the_default_deadline_is_the_contract_timeout() -> None:
    # descriptor.timeout_ms is 30000: 29 s left is refused, 31 s left is asked.
    port = _LiveFencePort({(_KIND, _RESOURCE): _LIVE_FENCE})
    inside = _RecordingTransport()
    result = await _handler(inside, port, deadline_seconds=None).handle(
        _req(EnumRepoWriteOperation.PR_COMMENT, _lease(ttl_s=29.0), dry_run=True)
    )
    _assert_refused(result, inside, EnumRepoWriteRefusal.LEASE_EXPIRED)
    result = await _handler(_RecordingTransport(), port, deadline_seconds=None).handle(
        _req(EnumRepoWriteOperation.PR_COMMENT, _lease(ttl_s=31.0), dry_run=True)
    )
    assert isinstance(result, ModelRepoWriteCompleted), result


# --- AC3: the fence port reading unknown refuses every write --------------------


@pytest.mark.parametrize("op", list(_VERB_FIELDS), ids=lambda op: op.value)
async def test_the_port_reading_unknown_refuses_every_verb(
    op: EnumRepoWriteOperation,
) -> None:
    transport = _RecordingTransport()
    port = _FixedPort(EnumRepoWriteFenceStatus.UNKNOWN)
    result = await _handler(transport, port).handle(_req(op, _lease()))
    _assert_refused(result, transport, EnumRepoWriteRefusal.LEASE_UNREADABLE)
    assert port.calls == 1


@pytest.mark.parametrize(
    "port",
    [None, _RaisingPort(), _FixedPort("not-a-status")],
    ids=["no-port", "port-raises", "port-answers-garbage"],
)
async def test_an_unreadable_fence_is_refused_lease_unreadable(
    port: ProtocolLeaseFence | None,
) -> None:
    transport = _RecordingTransport()
    result = await _handler(transport, port).handle(
        _req(EnumRepoWriteOperation.PR_CLOSE, _lease())
    )
    _assert_refused(result, transport, EnumRepoWriteRefusal.LEASE_UNREADABLE)


# --- the request model --------------------------------------------------------


def test_a_request_without_a_lease_serializes_without_the_field() -> None:
    # A runtime that predates the field forbids extra inputs.
    dumped = _req(EnumRepoWriteOperation.PR_COMMENT, None).model_dump(mode="json")
    assert "lease" not in dumped
    leased = _req(EnumRepoWriteOperation.PR_COMMENT, _lease()).model_dump(mode="json")
    assert leased["lease"]["fence"] == _LIVE_FENCE


def test_a_lease_needs_a_positive_fence_and_an_aware_expiry() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        _lease(fence=0)
    with pytest.raises(ValueError, match="timezone"):
        ModelRepoWriteLease(
            kind=_KIND,
            resource=_RESOURCE,
            fence=1,
            expires_at=datetime(2026, 10, 26, 5, 0, 0),
        )
    assert _lease().expires_at - datetime.fromtimestamp(_NOW, tz=UTC) == timedelta(
        seconds=600
    )
