# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerWebhookReconciler — OMN-8492, Component 1."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_webhook_reconciler import (
    HandlerWebhookReconciler,
)
from omnimarket.nodes.node_pr_review_bot.topics import TOPIC_THREAD_REOPENED

BOT_LOGIN = "onexbot[bot]"


def _make_handler(
    authorized_bypass_actors: list[str] | None = None,
    graphql: object | None = None,
    rest: object | None = None,
    kafka: object | None = None,
) -> HandlerWebhookReconciler:
    return HandlerWebhookReconciler(
        bot_login=BOT_LOGIN,
        github_graphql=graphql or MagicMock(),
        github_rest=rest or MagicMock(),
        kafka_publisher=kafka or MagicMock(),
        authorized_bypass_actors=authorized_bypass_actors,
    )


@pytest.mark.unit
class TestReconcilerBotResolution:
    def test_bot_resolution_is_allowed(self) -> None:
        handler = _make_handler()
        result = handler.handle(
            thread_id="TH_abc123",
            actor=BOT_LOGIN,
            pr_number=42,
            repo="OmniNode-ai/omnimarket",
            head_sha="deadbeef",
        )
        assert result.action_taken == "allowed"

    def test_bot_resolution_does_not_call_graphql(self) -> None:
        graphql = MagicMock()
        handler = _make_handler(graphql=graphql)
        handler.handle(
            thread_id="TH_abc123",
            actor=BOT_LOGIN,
            pr_number=42,
            repo="OmniNode-ai/omnimarket",
            head_sha="deadbeef",
        )
        graphql.execute.assert_not_called()

    def test_bot_resolution_does_not_emit_kafka(self) -> None:
        kafka = MagicMock()
        handler = _make_handler(kafka=kafka)
        handler.handle(
            thread_id="TH_abc123",
            actor=BOT_LOGIN,
            pr_number=42,
            repo="OmniNode-ai/omnimarket",
            head_sha="deadbeef",
        )
        kafka.publish.assert_not_called()


@pytest.mark.unit
class TestReconcilerUnauthorizedResolution:
    def test_worker_resolution_triggers_reopen(self) -> None:
        graphql = MagicMock()
        handler = _make_handler(graphql=graphql)
        result = handler.handle(
            thread_id="TH_def456",
            actor="worker-bot",
            pr_number=10,
            repo="OmniNode-ai/omnimarket",
            head_sha="cafebabe",
        )
        assert result.action_taken == "reopened"
        graphql.execute.assert_called_once()

    def test_coderabbit_resolution_triggers_reopen(self) -> None:
        graphql = MagicMock()
        handler = _make_handler(graphql=graphql)
        result = handler.handle(
            thread_id="TH_ghi789",
            actor="coderabbitai[bot]",
            pr_number=200,
            repo="OmniNode-ai/omnimarket",
            head_sha="aabbccdd",
        )
        assert result.action_taken == "reopened"
        graphql.execute.assert_called_once()

    def test_reopen_posts_comment(self) -> None:
        rest = MagicMock()
        handler = _make_handler(rest=rest)
        handler.handle(
            thread_id="TH_jkl012",
            actor="some-user",
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            head_sha="11223344",
        )
        rest.post_pr_comment.assert_called_once()
        _, call_kwargs = (
            rest.post_pr_comment.call_args[0],
            rest.post_pr_comment.call_args,
        )
        body = call_kwargs[0][2]
        assert "onex review bot" in body.lower()

    def test_reopen_emits_kafka_event(self) -> None:
        kafka = MagicMock()
        handler = _make_handler(kafka=kafka)
        handler.handle(
            thread_id="TH_mno345",
            actor="intruder",
            pr_number=77,
            repo="OmniNode-ai/omnimarket",
            head_sha="ffffffff",
        )
        kafka.publish.assert_called_once()
        topic, payload = kafka.publish.call_args[0]
        assert topic == TOPIC_THREAD_REOPENED
        assert payload["thread_id"] == "TH_mno345"
        assert payload["actor"] == "intruder"
        assert payload["pr_number"] == 77

    def test_result_contains_event_id_on_reopen(self) -> None:
        handler = _make_handler()
        result = handler.handle(
            thread_id="TH_pqr678",
            actor="bad-actor",
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            head_sha="00000000",
        )
        assert result.action_taken == "reopened"
        assert result.event_id is not None


@pytest.mark.unit
class TestReconcilerEmergencyBypass:
    def test_authorized_bypass_actor_is_allowed(self) -> None:
        handler = _make_handler(authorized_bypass_actors=["jonahgabriel"])
        result = handler.handle(
            thread_id="TH_stu901",
            actor="jonahgabriel",
            pr_number=99,
            repo="OmniNode-ai/omnimarket",
            head_sha="12345678",
        )
        assert result.action_taken == "bypass_allowed"

    def test_unauthorized_bypass_actor_is_reopened(self) -> None:
        handler = _make_handler(authorized_bypass_actors=["jonahgabriel"])
        result = handler.handle(
            thread_id="TH_vwx234",
            actor="someone-else",
            pr_number=99,
            repo="OmniNode-ai/omnimarket",
            head_sha="12345678",
        )
        assert result.action_taken == "reopened"

    def test_empty_bypass_actors_means_no_bypass(self) -> None:
        graphql = MagicMock()
        handler = _make_handler(authorized_bypass_actors=[], graphql=graphql)
        result = handler.handle(
            thread_id="TH_yza567",
            actor="jonahgabriel",
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            head_sha="99887766",
        )
        assert result.action_taken == "reopened"
        graphql.execute.assert_called_once()


@pytest.mark.unit
class TestReconcilerGraphQLQuery:
    def test_reopen_uses_correct_graphql_mutation(self) -> None:
        graphql = MagicMock()
        handler = _make_handler(graphql=graphql)
        handler.handle(
            thread_id="TH_bcd890",
            actor="someone",
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            head_sha="aabbccdd",
        )
        query, variables = graphql.execute.call_args[0]
        assert "reopenPullRequestReviewThread" in query
        assert variables["threadId"] == "TH_bcd890"
