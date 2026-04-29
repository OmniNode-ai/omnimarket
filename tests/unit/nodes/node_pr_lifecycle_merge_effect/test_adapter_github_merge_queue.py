# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for GitHubMergeQueueAdapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest

from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.adapter_github_merge_queue import (
    GitHubMergeQueueAdapter,
)


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", rc: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = rc
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.fixture
def subprocess_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[list[_FakeProc]], list[list[str]]]:
    def install(queue: list[_FakeProc]) -> list[list[str]]:
        calls: list[list[str]] = []

        async def fake_exec(*argv: str, **_kwargs: object) -> _FakeProc:
            calls.append(list(argv))
            if not queue:
                return _FakeProc()
            return queue.pop(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return calls

    return install


@pytest.mark.unit
class TestGitHubMergeQueueAdapter:
    async def test_merge_queue_path_enables_and_enqueues(
        self, subprocess_recorder: Callable[[list[_FakeProc]], list[list[str]]]
    ) -> None:
        calls = subprocess_recorder(
            [
                _FakeProc(stdout=json.dumps({"id": "PR_node_1"}).encode()),
                _FakeProc(stdout=b'{"data":{"enablePullRequestAutoMerge":{}}}'),
                _FakeProc(
                    stdout=b'{"data":{"enqueuePullRequest":{"mergeQueueEntry":{"position":2}}}}'
                ),
            ]
        )

        adapter = GitHubMergeQueueAdapter()
        result = await adapter.merge_pr("OmniNode-ai/omnimarket", 42, True)

        assert "enqueued OmniNode-ai/omnimarket#42 at position 2" in result
        assert calls[0] == [
            "gh",
            "pr",
            "view",
            "42",
            "--repo",
            "OmniNode-ai/omnimarket",
            "--json",
            "id",
        ]
        assert calls[1][:3] == ["gh", "api", "graphql"]
        assert "method=SQUASH" in calls[1]
        assert any("enablePullRequestAutoMerge" in arg for arg in calls[1])
        assert calls[2][:3] == ["gh", "api", "graphql"]
        assert any("enqueuePullRequest" in arg for arg in calls[2])

    async def test_non_queue_path_does_not_enqueue(
        self, subprocess_recorder: Callable[[list[_FakeProc]], list[list[str]]]
    ) -> None:
        calls = subprocess_recorder(
            [
                _FakeProc(stdout=json.dumps({"id": "PR_node_1"}).encode()),
                _FakeProc(stdout=b'{"data":{"enablePullRequestAutoMerge":{}}}'),
            ]
        )

        adapter = GitHubMergeQueueAdapter()
        result = await adapter.merge_pr("OmniNode-ai/omnimarket", 42, False)

        assert result == "auto-merge enabled for OmniNode-ai/omnimarket#42"
        assert len(calls) == 2
        assert not any("enqueuePullRequest" in " ".join(call) for call in calls)

    async def test_no_merge_queue_marker_is_nonfatal(
        self, subprocess_recorder: Callable[[list[_FakeProc]], list[list[str]]]
    ) -> None:
        subprocess_recorder(
            [
                _FakeProc(stdout=json.dumps({"id": "PR_node_1"}).encode()),
                _FakeProc(stdout=b'{"data":{"enablePullRequestAutoMerge":{}}}'),
                _FakeProc(
                    stderr=b"GraphQL: Base branch does not have a merge queue enabled",
                    rc=1,
                ),
            ]
        )

        adapter = GitHubMergeQueueAdapter()
        result = await adapter.merge_pr("OmniNode-ai/small-repo", 7, True)

        assert (
            result
            == "auto-merge enabled for OmniNode-ai/small-repo#7; repo has no merge queue"
        )

    async def test_post_pr_comment_uses_gh_comment(
        self, subprocess_recorder: Callable[[list[_FakeProc]], list[list[str]]]
    ) -> None:
        calls = subprocess_recorder([_FakeProc()])

        adapter = GitHubMergeQueueAdapter()
        await adapter.post_pr_comment("OmniNode-ai/omnimarket", 42, "body")

        assert calls == [
            [
                "gh",
                "pr",
                "comment",
                "42",
                "--repo",
                "OmniNode-ai/omnimarket",
                "--body",
                "body",
            ]
        ]
