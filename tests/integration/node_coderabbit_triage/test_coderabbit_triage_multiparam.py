# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""WS-5 Wave 3 — multi-parameter integration coverage for node_coderabbit_triage.

COMPUTE node (Variant A): the handler is driven in-process. Two I/O seams are
mocked via injected collaborators (never subprocess monkeypatch):

  * the GraphQL *read* (review-thread fetch) is overridden via a subclass that
    returns synthetic raw thread node dicts — the same shape the live
    ``_fetch_review_threads`` returns from ``gh api graphql``;
  * the GraphQL *write* (reply + resolveReviewThread) is an injected
    ``ProtocolGhApi`` recorder so each reply/resolve call is captured.

Each parametrized case asserts the TYPED ``ModelCoderabbitTriageResult`` counts
(total / blocking / suggestion / unknown / resolved) AND the recorded write
calls. Negative control: a BLOCKING thread must be classified BLOCKING, never
auto-resolved (zero write calls), and ``has_blockers`` must be True.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_coderabbit_triage.handlers.handler_coderabbit_triage import (
    EnumThreadSeverity,
    HandlerCoderabbitTriage,
    ModelCoderabbitTriageCommand,
    ModelCoderabbitTriageResult,
)

REPO = "OmniNode-ai/omnimarket"
PR = 1234


class _RecordingGhApi:
    """ProtocolGhApi recorder — captures reply/resolve calls without any I/O."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, int, int]] = []
        self.resolved: list[str] = []

    def reply_to_thread(
        self, *, repo: str, pull_number: int, comment_id: int, body: str
    ) -> None:
        self.replies.append((repo, pull_number, comment_id))

    def resolve_review_thread(self, *, thread_id: str) -> None:
        self.resolved.append(thread_id)


def _thread(
    *,
    thread_id: str,
    comment_id: int,
    body: str,
    author: str = "coderabbitai[bot]",
    resolved: bool = False,
) -> dict:  # type: ignore[type-arg]
    """Build one raw reviewThreads GraphQL node (the live fetch shape)."""
    return {
        "id": thread_id,
        "isResolved": resolved,
        "comments": {
            "nodes": [
                {
                    "databaseId": comment_id,
                    "author": {"login": author},
                    "body": body,
                    "path": "src/x.py",
                    "url": f"https://github.com/{REPO}/pull/{PR}#r{comment_id}",
                }
            ]
        },
    }


class _StubFetchHandler(HandlerCoderabbitTriage):
    """Override the GraphQL read seam with synthetic raw thread nodes."""

    def __init__(self, raw_threads: list[dict], gh_api: _RecordingGhApi) -> None:  # type: ignore[type-arg]
        super().__init__(gh_api=gh_api)
        self._raw_threads = raw_threads

    def _fetch_review_threads(self, owner: str, repo: str, pr_number: int):  # type: ignore[no-untyped-def]
        return self._raw_threads


# Each case: (raw threads, dry_run, expected counts, expected write-call count).
_SUGGESTION = _thread(
    thread_id="T_sug", comment_id=11, body="nitpick: minor style, consider renaming"
)
_BLOCKING = _thread(
    thread_id="T_blk", comment_id=22, body="This is a critical security bug, must fix"
)
_ALREADY_RESOLVED_SUGGESTION = _thread(
    thread_id="T_done", comment_id=33, body="typo here", resolved=True
)
_NON_CODERABBIT = _thread(
    thread_id="T_hum", comment_id=44, body="nit: rename", author="some-human"
)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("raw_threads", "dry_run", "expect"),
    [
        pytest.param(
            [],
            False,
            {
                "total": 0,
                "blocking": 0,
                "suggestion": 0,
                "resolved": 0,
                "writes": 0,
                "has_blockers": False,
            },
            id="no-threads",
        ),
        pytest.param(
            [_SUGGESTION],
            False,
            {
                "total": 1,
                "blocking": 0,
                "suggestion": 1,
                "resolved": 1,
                "writes": 1,
                "has_blockers": False,
            },
            id="suggestion-auto-resolved",
        ),
        # NEGATIVE CONTROL: a BLOCKING thread is detected and never resolved.
        pytest.param(
            [_BLOCKING],
            False,
            {
                "total": 1,
                "blocking": 1,
                "suggestion": 0,
                "resolved": 0,
                "writes": 0,
                "has_blockers": True,
            },
            id="blocking-left-open",
        ),
        # dry_run: suggestion classified but no write calls, resolved_count == 0.
        pytest.param(
            [_SUGGESTION],
            True,
            {
                "total": 1,
                "blocking": 0,
                "suggestion": 1,
                "resolved": 0,
                "writes": 0,
                "has_blockers": False,
            },
            id="suggestion-dry-run-no-write",
        ),
        # Mixed batch: blocking + suggestion + already-resolved + non-coderabbit.
        # Non-coderabbit author is filtered out entirely.
        pytest.param(
            [_BLOCKING, _SUGGESTION, _ALREADY_RESOLVED_SUGGESTION, _NON_CODERABBIT],
            False,
            {
                "total": 3,
                "blocking": 1,
                "suggestion": 2,
                "resolved": 1,
                "writes": 1,
                "has_blockers": True,
            },
            id="mixed-batch",
        ),
    ],
)
async def test_coderabbit_triage_multiparam(
    raw_threads: list[dict],  # type: ignore[type-arg]
    dry_run: bool,
    expect: dict[str, object],
) -> None:
    gh_api = _RecordingGhApi()
    handler = _StubFetchHandler(raw_threads, gh_api)
    command = ModelCoderabbitTriageCommand(
        repo=REPO,
        pr_number=PR,
        correlation_id="cid-coderabbit",
        dry_run=dry_run,
    )

    result = handler.handle(command)

    assert isinstance(result, ModelCoderabbitTriageResult)
    assert result.repo == REPO
    assert result.pr_number == PR
    assert result.dry_run is dry_run
    assert result.total_threads == expect["total"]
    assert result.blocking_count == expect["blocking"]
    assert result.suggestion_count == expect["suggestion"]
    assert result.resolved_count == expect["resolved"]
    assert result.has_blockers is expect["has_blockers"]
    # The write boundary was exercised exactly when expected.
    assert len(gh_api.resolved) == expect["writes"]
    assert len(gh_api.replies) == expect["writes"]
    # Every blocking thread keeps severity BLOCKING and acted == False.
    for thread in result.threads:
        if thread.severity == EnumThreadSeverity.BLOCKING:
            assert thread.acted is False


@pytest.mark.integration
def test_classify_body_pure_keyword_matching() -> None:
    """Pure classifier: blocking keywords win over suggestion keywords."""
    handler = HandlerCoderabbitTriage(gh_api=_RecordingGhApi())
    sev_block, kw_block = handler.classify_body("nitpick but this is a critical bug")
    sev_sug, _ = handler.classify_body("consider renaming for readability")
    sev_unknown, kw_unknown = handler.classify_body("looks good to me")
    assert sev_block == EnumThreadSeverity.BLOCKING
    assert kw_block != ""
    assert sev_sug == EnumThreadSeverity.SUGGESTION
    assert sev_unknown == EnumThreadSeverity.UNKNOWN
    assert kw_unknown == ""
