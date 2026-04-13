# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerCommitCitationVerifier — OMN-8492, Component 2."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_commit_citation_verifier import (
    STATUS_CONTEXT,
    HandlerCommitCitationVerifier,
)
from omnimarket.nodes.node_pr_review_bot.topics import TOPIC_THREAD_RESOLVED

BOT_LOGIN = "onexbot[bot]"


def _make_handler(
    reviewer_passes: bool = True,
    graphql: object | None = None,
    rest: object | None = None,
    kafka: object | None = None,
) -> HandlerCommitCitationVerifier:
    reviewer = MagicMock()
    reviewer.verify.return_value = reviewer_passes
    return HandlerCommitCitationVerifier(
        bot_login=BOT_LOGIN,
        hostile_reviewer=reviewer,
        github_graphql=graphql or MagicMock(),
        github_rest=rest or MagicMock(),
        kafka_publisher=kafka or MagicMock(),
    )


@pytest.mark.unit
class TestCitationParsing:
    def test_fixes_pattern_matched(self) -> None:
        handler = _make_handler()
        result = handler.process_commit(
            pr_number=10,
            repo="OmniNode-ai/omnimarket",
            sha="abc123",
            commit_messages=["Fixes TH_thread1 — added null check"],
            open_thread_ids=["TH_thread1"],
            thread_findings={"TH_thread1": "Null pointer in handler"},
            diff_by_thread={"TH_thread1": "+  if x is None: return"},
        )
        assert len(result.citations_found) == 1
        assert result.citations_found[0].thread_id == "TH_thread1"

    def test_resolves_pattern_matched(self) -> None:
        handler = _make_handler()
        result = handler.process_commit(
            pr_number=10,
            repo="OmniNode-ai/omnimarket",
            sha="abc123",
            commit_messages=["Resolves TH_thread2"],
            open_thread_ids=["TH_thread2"],
            thread_findings={},
            diff_by_thread={},
        )
        assert any(c.thread_id == "TH_thread2" for c in result.citations_found)

    def test_addressed_in_commit_pattern_matched(self) -> None:
        handler = _make_handler()
        result = handler.process_commit(
            pr_number=10,
            repo="OmniNode-ai/omnimarket",
            sha="deadbeef12345",
            commit_messages=["Addressed in commit deadbeef12345"],
            open_thread_ids=["deadbeef12345"],
            thread_findings={},
            diff_by_thread={},
        )
        assert any(c.thread_id == "deadbeef12345" for c in result.citations_found)

    def test_citation_for_closed_thread_ignored(self) -> None:
        handler = _make_handler()
        result = handler.process_commit(
            pr_number=10,
            repo="OmniNode-ai/omnimarket",
            sha="abc123",
            commit_messages=["Fixes TH_already_closed"],
            open_thread_ids=["TH_open"],
            thread_findings={},
            diff_by_thread={},
        )
        assert result.citations_found == []

    def test_no_citations_in_commit(self) -> None:
        handler = _make_handler()
        result = handler.process_commit(
            pr_number=10,
            repo="OmniNode-ai/omnimarket",
            sha="abc123",
            commit_messages=["refactor: clean up imports"],
            open_thread_ids=["TH_open"],
            thread_findings={},
            diff_by_thread={},
        )
        assert result.citations_found == []

    def test_duplicate_citation_deduplicated(self) -> None:
        handler = _make_handler()
        result = handler.process_commit(
            pr_number=10,
            repo="OmniNode-ai/omnimarket",
            sha="abc123",
            commit_messages=[
                "Fixes TH_thread1 — first commit",
                "Resolves TH_thread1 — duplicate",
            ],
            open_thread_ids=["TH_thread1"],
            thread_findings={},
            diff_by_thread={},
        )
        assert len(result.citations_found) == 1


@pytest.mark.unit
class TestVerificationOutcomes:
    def test_passing_verification_resolves_thread(self) -> None:
        graphql = MagicMock()
        kafka = MagicMock()
        handler = _make_handler(reviewer_passes=True, graphql=graphql, kafka=kafka)
        result = handler.process_commit(
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            sha="def456",
            commit_messages=["Fixes TH_pass_me"],
            open_thread_ids=["TH_pass_me"],
            thread_findings={"TH_pass_me": "Missing error handling"},
            diff_by_thread={"TH_pass_me": "+  except Exception as e: log(e)"},
        )
        assert len(result.thread_results) == 1
        assert result.thread_results[0].resolved is True
        graphql.execute.assert_called_once()

    def test_failing_verification_posts_reply_not_resolves(self) -> None:
        graphql = MagicMock()
        rest = MagicMock()
        handler = _make_handler(reviewer_passes=False, graphql=graphql, rest=rest)
        result = handler.process_commit(
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            sha="def456",
            commit_messages=["Fixes TH_fail_me"],
            open_thread_ids=["TH_fail_me"],
            thread_findings={"TH_fail_me": "Security vulnerability"},
            diff_by_thread={"TH_fail_me": "+  pass"},
        )
        assert result.thread_results[0].resolved is False
        graphql.execute.assert_not_called()
        rest.post_thread_reply.assert_called_once()

    def test_resolved_thread_emits_kafka_event(self) -> None:
        kafka = MagicMock()
        handler = _make_handler(reviewer_passes=True, kafka=kafka)
        handler.process_commit(
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            sha="def456",
            commit_messages=["Fixes TH_emit_me"],
            open_thread_ids=["TH_emit_me"],
            thread_findings={"TH_emit_me": "issue"},
            diff_by_thread={"TH_emit_me": "diff"},
        )
        kafka.publish.assert_called_once()
        topic, payload = kafka.publish.call_args[0]
        assert topic == TOPIC_THREAD_RESOLVED
        assert payload["thread_id"] == "TH_emit_me"
        assert payload["resolved_by"] == BOT_LOGIN

    def test_failed_verification_does_not_emit_kafka(self) -> None:
        kafka = MagicMock()
        handler = _make_handler(reviewer_passes=False, kafka=kafka)
        handler.process_commit(
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            sha="def456",
            commit_messages=["Fixes TH_no_emit"],
            open_thread_ids=["TH_no_emit"],
            thread_findings={},
            diff_by_thread={},
        )
        kafka.publish.assert_not_called()


@pytest.mark.unit
class TestCommitStatusUpdate:
    def test_all_resolved_posts_success_status(self) -> None:
        rest = MagicMock()
        handler = _make_handler(reviewer_passes=True, rest=rest)
        result = handler.process_commit(
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            sha="sha1",
            commit_messages=["Fixes TH_only"],
            open_thread_ids=["TH_only"],
            thread_findings={},
            diff_by_thread={},
        )
        assert result.all_resolved is True
        rest.post_commit_status.assert_called_once()
        call_args = (
            rest.post_commit_status.call_args[1]
            if rest.post_commit_status.call_args[1]
            else {}
        )
        # positional args: repo, sha, state, context, description
        pos_args = rest.post_commit_status.call_args[0]
        state = call_args.get("state") or pos_args[2]
        context = call_args.get("context") or pos_args[3]
        assert state == "success"
        assert context == STATUS_CONTEXT

    def test_unresolved_posts_failure_status(self) -> None:
        rest = MagicMock()
        handler = _make_handler(reviewer_passes=False, rest=rest)
        result = handler.process_commit(
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            sha="sha1",
            commit_messages=["Fixes TH_still_open"],
            open_thread_ids=["TH_still_open"],
            thread_findings={},
            diff_by_thread={},
        )
        assert result.all_resolved is False
        rest.post_commit_status.assert_called_once()
        pos_args = rest.post_commit_status.call_args[0]
        state = pos_args[2]
        assert state == "failure"

    def test_no_citations_still_updates_status(self) -> None:
        rest = MagicMock()
        handler = _make_handler(rest=rest)
        handler.process_commit(
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            sha="sha1",
            commit_messages=["chore: bump deps"],
            open_thread_ids=["TH_still_open"],
            thread_findings={},
            diff_by_thread={},
        )
        rest.post_commit_status.assert_called_once()

    def test_no_open_threads_posts_success_status(self) -> None:
        rest = MagicMock()
        handler = _make_handler(rest=rest)
        result = handler.process_commit(
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            sha="sha1",
            commit_messages=["chore: no threads"],
            open_thread_ids=[],
            thread_findings={},
            diff_by_thread={},
        )
        assert result.all_resolved is True
        pos_args = rest.post_commit_status.call_args[0]
        state = pos_args[2]
        assert state == "success"
