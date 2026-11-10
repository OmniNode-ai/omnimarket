# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A reused idempotency key never returns another request's result (OMN-23559).

The handler's record of finished writes is keyed by repository, operation and
idempotency key. Before this change a later request with the same key and
different content got the first request's result back as a replay, including
the first request's body. Now the record carries a digest per request field and
a key reused for different content is refused with ``idempotency_key_conflict``,
naming the fields that differ and never their values.

Every failing control below fails at the base commit: the second request comes
back ``replayed`` with the first request's planned body.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.github_landing.github_landing_requests import (
    edit_pull_request_request,
    pull_request_request,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from tests.unit.nodes.node_github_repo_write_effect.test_handler_github_repo_write import (
    _HEAD,
    _REPO,
    _handler,
    _ok,
    _pr,
    _Recorded,
    _req,
)

pytestmark = pytest.mark.unit

_KEY = "reused-key"
_OTHER_CORRELATION = UUID("00000000-0000-4000-8000-000000023559")
_ALPHA = "ALPHA private text of the first request"
_BRAVO = "BRAVO text of the second request"


def _first_edit_exchanges() -> _Recorded:
    return _Recorded(
        [
            (pull_request_request(_REPO, 4000), _ok(_pr())),
            (
                edit_pull_request_request(_REPO, 4000, {"body": _ALPHA}),
                _ok(_pr()),
            ),
        ]
    )


def _edit(**kw: object) -> ModelRepoWriteRequest:
    fields: dict[str, object] = {
        "pr_number": 4000,
        "expected_head_sha": _HEAD,
        "body": _ALPHA,
        "idempotency_key": _KEY,
        **kw,
    }
    return _req(EnumRepoWriteOperation.PR_EDIT, **fields)


async def test_a_reused_key_with_a_different_body_is_refused_not_replayed() -> None:
    transport = _first_edit_exchanges()
    handler = _handler(transport)
    first = await handler.handle(_edit())
    assert isinstance(first, ModelRepoWriteCompleted)
    sent_after_first = len(transport.sent)

    second = await handler.handle(_edit(**{"body": _BRAVO}))

    assert isinstance(second, ModelRepoWriteFailed)
    assert second.reason is EnumRepoWriteRefusal.IDEMPOTENCY_KEY_CONFLICT
    assert "body" in second.detail
    assert _KEY in second.detail
    assert _ALPHA not in second.detail
    assert _BRAVO not in second.detail
    assert len(transport.sent) == sent_after_first


async def test_a_reused_key_with_a_different_body_is_refused_in_a_dry_run() -> None:
    transport = _first_edit_exchanges()
    handler = _handler(transport)
    assert isinstance(await handler.handle(_edit()), ModelRepoWriteCompleted)

    second = await handler.handle(_edit(**{"body": _BRAVO, "dry_run": True}))

    assert isinstance(second, ModelRepoWriteFailed)
    assert second.reason is EnumRepoWriteRefusal.IDEMPOTENCY_KEY_CONFLICT
    assert second.mode is EnumRepoWriteMode.DRY_RUN
    assert second.requests == ()


async def test_a_reused_key_on_a_different_pull_request_is_refused() -> None:
    transport = _first_edit_exchanges()
    handler = _handler(transport)
    assert isinstance(await handler.handle(_edit()), ModelRepoWriteCompleted)

    second = await handler.handle(_edit(**{"pr_number": 4001}))

    assert isinstance(second, ModelRepoWriteFailed)
    assert second.reason is EnumRepoWriteRefusal.IDEMPOTENCY_KEY_CONFLICT
    assert "pr_number" in second.detail
    assert "body" not in second.detail


async def test_a_true_retry_with_a_new_correlation_and_lane_still_replays() -> None:
    transport = _first_edit_exchanges()
    handler = _handler(transport)
    first = await handler.handle(_edit())
    assert isinstance(first, ModelRepoWriteCompleted)
    sent_after_first = len(transport.sent)

    retry = await handler.handle(
        _edit(
            correlation_id=_OTHER_CORRELATION,
            requested_by_lane="another-lane-0001",
            expected_head_sha="b" * 40,
        )
    )

    assert isinstance(retry, ModelRepoWriteCompleted)
    assert retry.replayed is True
    assert retry.correlation_id == _OTHER_CORRELATION
    assert retry.requests == first.requests
    assert len(transport.sent) == sent_after_first


async def test_a_refused_conflict_does_not_replace_the_recorded_write() -> None:
    transport = _first_edit_exchanges()
    handler = _handler(transport)
    assert isinstance(await handler.handle(_edit()), ModelRepoWriteCompleted)
    assert isinstance(
        await handler.handle(_edit(**{"body": _BRAVO})), ModelRepoWriteFailed
    )

    again = await handler.handle(_edit())

    assert isinstance(again, ModelRepoWriteCompleted)
    assert again.replayed is True
