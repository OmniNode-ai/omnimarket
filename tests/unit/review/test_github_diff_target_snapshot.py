# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-21189): the diff effect returns head sha, base sha and diff digest read
together; a push between the reads yields an unavailable snapshot with a reason."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from omnimarket.models.model_review_target_snapshot import (
    EnumReviewSnapshotStatus,
    ModelReviewTargetSnapshot,
    diff_digest,
)
from omnimarket.nodes.node_github_diff_effect.handlers import handler_github_diff
from omnimarket.nodes.node_github_diff_effect.handlers.handler_github_diff import (
    HandlerGithubDiffEffect,
)
from omnimarket.review.node_io import (
    ModelGithubDiffCommand,
    ModelGithubDiffResolvedEvent,
)

_DIFF = "diff --git a/foo.py b/foo.py\n+print('x')\n"
_HEAD = "a" * 40
_BASE = "b" * 40
_MOVED_HEAD = "c" * 40


class _FixtureDiffEffect(HandlerGithubDiffEffect):
    """Serves a scripted sequence of (head, base) reads around one diff read."""

    def __init__(self, reads: list[tuple[str, str] | Exception]) -> None:
        self._reads = list(reads)
        self.calls: list[str] = []

    def _fetch_pr_refs(self, repo: str, pr_number: int, token: str) -> tuple[str, str]:
        self.calls.append("refs")
        read = self._reads.pop(0)
        if isinstance(read, Exception):
            raise read
        return read

    def _fetch_pr_diff(self, repo: str, pr_number: int, token: str) -> str:
        self.calls.append("diff")
        return _DIFF


@pytest.fixture(autouse=True)
def _github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve(_ref: str) -> SecretStr:
        return SecretStr("fixture-token")

    monkeypatch.setattr(handler_github_diff, "resolve_api_key_async", _resolve)


async def _resolve_pr(effect: HandlerGithubDiffEffect) -> ModelGithubDiffResolvedEvent:
    output = await effect.handle(
        ModelGithubDiffCommand(correlation_id=uuid4(), repo="o/r", pr_number=7)
    )
    event = output.events[0]
    assert isinstance(event, ModelGithubDiffResolvedEvent)
    return event


@pytest.mark.asyncio
async def test_stable_fixture_returns_head_base_and_digest_together() -> None:
    effect = _FixtureDiffEffect([(_HEAD, _BASE), (_HEAD, _BASE)])

    event = await _resolve_pr(effect)

    assert event.content == _DIFF
    assert event.snapshot == ModelReviewTargetSnapshot(
        status=EnumReviewSnapshotStatus.ESTABLISHED,
        head_sha=_HEAD,
        base_sha=_BASE,
        diff_digest=diff_digest(_DIFF),
    )
    assert effect.calls == ["refs", "diff", "refs"]


@pytest.mark.asyncio
async def test_head_moved_between_reads_is_unavailable_with_reason() -> None:
    effect = _FixtureDiffEffect([(_HEAD, _BASE), (_MOVED_HEAD, _BASE)])

    event = await _resolve_pr(effect)

    snapshot = event.snapshot
    assert snapshot is not None
    assert snapshot.status is EnumReviewSnapshotStatus.UNAVAILABLE
    assert snapshot.unavailable_reason is not None
    assert "moved" in snapshot.unavailable_reason
    # never filled from either read
    assert (snapshot.head_sha, snapshot.base_sha, snapshot.diff_digest) == (
        None,
        None,
        None,
    )
    assert event.content == _DIFF


@pytest.mark.asyncio
async def test_base_moved_between_reads_is_unavailable() -> None:
    effect = _FixtureDiffEffect([(_HEAD, _BASE), (_HEAD, "d" * 40)])

    event = await _resolve_pr(effect)

    assert event.snapshot is not None
    assert event.snapshot.status is EnumReviewSnapshotStatus.UNAVAILABLE


@pytest.mark.parametrize("failing_read", [0, 1])
@pytest.mark.asyncio
async def test_failed_ref_read_is_unavailable_with_reason(failing_read: int) -> None:
    reads: list[tuple[str, str] | Exception] = [(_HEAD, _BASE), (_HEAD, _BASE)]
    reads[failing_read] = RuntimeError("502 bad gateway")
    effect = _FixtureDiffEffect(reads)

    event = await _resolve_pr(effect)

    assert event.snapshot is not None
    assert event.snapshot.status is EnumReviewSnapshotStatus.UNAVAILABLE
    assert event.snapshot.unavailable_reason is not None
    assert "502 bad gateway" in event.snapshot.unavailable_reason
    assert event.content == _DIFF


@pytest.mark.asyncio
async def test_file_target_has_no_head_so_snapshot_is_unavailable(
    tmp_path: Path,
) -> None:
    target = tmp_path / "plan.md"
    target.write_text("# plan\n", encoding="utf-8")

    output = await HandlerGithubDiffEffect().handle(
        ModelGithubDiffCommand(correlation_id=uuid4(), file_path=str(target))
    )

    event = output.events[0]
    assert isinstance(event, ModelGithubDiffResolvedEvent)
    assert event.snapshot is not None
    assert event.snapshot.status is EnumReviewSnapshotStatus.UNAVAILABLE


@pytest.mark.parametrize(
    "build",
    [
        lambda: ModelReviewTargetSnapshot(
            status=EnumReviewSnapshotStatus.ESTABLISHED, head_sha=_HEAD
        ),
        lambda: ModelReviewTargetSnapshot(
            status=EnumReviewSnapshotStatus.UNAVAILABLE, unavailable_reason=None
        ),
        lambda: ModelReviewTargetSnapshot(
            status=EnumReviewSnapshotStatus.UNAVAILABLE,
            unavailable_reason="x",
            head_sha=_HEAD,
        ),
    ],
    ids=["established-missing-fields", "unavailable-no-reason", "unavailable-with-sha"],
)
def test_snapshot_refuses_a_partial_or_mixed_shape(
    build: Callable[[], ModelReviewTargetSnapshot],
) -> None:
    with pytest.raises(ValueError, match="snapshot"):
        build()
