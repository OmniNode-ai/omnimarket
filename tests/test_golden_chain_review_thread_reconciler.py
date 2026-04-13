"""Golden chain tests for node_review_thread_reconciler.

TDD-first: these tests are written before the implementation.

Tests verify:
(a) non-bot actor triggers re-open + comment post
(b) bot actor is allowed through without re-open
(c) designated emergency-bypass actor is allowed through
(d) Kafka event emitted on re-open
(e) comment contains policy reminder text
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_review_thread_reconciler.handlers.handler_review_thread_reconciler import (
    HandlerReviewThreadReconciler,
    ModelReviewThreadReconcileCommand,
)
from omnimarket.nodes.node_review_thread_reconciler.protocols.protocol_github_client import (
    ProtocolGitHubReviewClient,
)

CMD_TOPIC = "onex.cmd.omnimarket.review-thread-reconcile.v1"
EVT_TOPIC = "onex.evt.omnimarket.review-thread-reopened.v1"

_ALLOWED_BOT = "pr-review-bot[bot]"
_BYPASS_USER = "jonahgabriel"


def _make_command(
    resolved_by: str,
    thread_id: str = "PRRT_test123",
    pr_node_id: str = "PR_test456",
    repo: str = "OmniNode-ai/omniclaude",
    pr_number: int = 42,
    allowed_actors: list[str] | None = None,
) -> ModelReviewThreadReconcileCommand:
    return ModelReviewThreadReconcileCommand(
        thread_id=thread_id,
        pr_node_id=pr_node_id,
        repo=repo,
        pr_number=pr_number,
        resolved_by=resolved_by,
        correlation_id="test-corr-001",
        allowed_actors=allowed_actors or [_ALLOWED_BOT, _BYPASS_USER],
    )


@pytest.mark.unit
class TestReviewThreadReconcilerGoldenChain:
    """Golden chain: reconciler logic + event bus wiring."""

    def _make_mock_client(self) -> MagicMock:
        client = MagicMock(spec=ProtocolGitHubReviewClient)
        client.unresolve_thread.return_value = True
        client.post_comment.return_value = True
        return client

    # (a) non-bot actor triggers re-open + comment post
    async def test_reconciler_reopens_non_bot_resolution(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Non-bot resolution triggers unresolve_thread + post_comment."""
        client = self._make_mock_client()
        handler = HandlerReviewThreadReconciler(github_client=client)
        command = _make_command(resolved_by="some-human-dev")

        result = handler.handle(command)

        assert result.reopened is True
        client.unresolve_thread.assert_called_once_with(command.thread_id)
        client.post_comment.assert_called_once()

    # (b) bot actor is allowed through without re-open
    async def test_reconciler_leaves_bot_resolution_alone(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Bot resolution is allowed — no unresolve, no comment."""
        client = self._make_mock_client()
        handler = HandlerReviewThreadReconciler(github_client=client)
        command = _make_command(resolved_by=_ALLOWED_BOT)

        result = handler.handle(command)

        assert result.reopened is False
        client.unresolve_thread.assert_not_called()
        client.post_comment.assert_not_called()

    # (c) designated emergency-bypass actor is allowed through
    async def test_reconciler_allows_emergency_bypass_actor(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Emergency-bypass actor in allow-list is allowed through."""
        client = self._make_mock_client()
        handler = HandlerReviewThreadReconciler(github_client=client)
        command = _make_command(resolved_by=_BYPASS_USER)

        result = handler.handle(command)

        assert result.reopened is False
        client.unresolve_thread.assert_not_called()
        client.post_comment.assert_not_called()

    # (e) comment contains policy reminder pointing at fix-commit-cite workflow
    async def test_reconciler_comment_contains_policy_reminder(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Re-open comment includes text directing user to push a fix commit."""
        client = self._make_mock_client()
        handler = HandlerReviewThreadReconciler(github_client=client)
        command = _make_command(resolved_by="unauthorized-human")

        handler.handle(command)

        call_args = client.post_comment.call_args
        comment_text: str = (
            call_args[0][1] if call_args[0] else call_args[1].get("body", "")
        )
        assert "fix" in comment_text.lower()
        assert "commit" in comment_text.lower() or "bot" in comment_text.lower()

    # (d) Kafka event emitted on re-open via event bus
    async def test_reconciler_emits_kafka_event_on_reopen(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Re-opening a thread publishes to thread_reopened Kafka topic."""
        client = self._make_mock_client()
        handler = HandlerReviewThreadReconciler(
            github_client=client, event_bus=event_bus
        )
        command = _make_command(resolved_by="some-human-dev")

        await event_bus.start()
        result = handler.handle(command)
        await handler.emit_event(command, result)

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1
        event_payload = json.loads(history[0].value)
        assert event_payload["thread_id"] == command.thread_id
        assert event_payload["reopened_by"] == command.resolved_by
        assert result.reopened is True

        await event_bus.close()

    # case-insensitive actor comparison
    async def test_actor_comparison_is_case_insensitive(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Actor login comparison is case-insensitive per ticket spec."""
        client = self._make_mock_client()
        handler = HandlerReviewThreadReconciler(github_client=client)
        # Bot login in uppercase — should still be allowed
        command = _make_command(resolved_by=_ALLOWED_BOT.upper())

        result = handler.handle(command)

        assert result.reopened is False
        client.unresolve_thread.assert_not_called()

    # configurable allow-list: extra user added dynamically
    async def test_configurable_allow_list_extra_actor(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Allow-list can be extended without code change; extra actor is not reopened."""
        client = self._make_mock_client()
        handler = HandlerReviewThreadReconciler(github_client=client)
        command = _make_command(
            resolved_by="emergency-admin",
            allowed_actors=[_ALLOWED_BOT, _BYPASS_USER, "emergency-admin"],
        )

        result = handler.handle(command)

        assert result.reopened is False
        client.unresolve_thread.assert_not_called()

    # full event bus wiring: CMD topic → handler → EVT topic
    async def test_event_bus_wiring_cmd_to_evt(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Full event bus wiring: command consumed, result event published."""
        client = self._make_mock_client()
        handler = HandlerReviewThreadReconciler(
            github_client=client, event_bus=event_bus
        )
        results_captured: list[dict] = []  # type: ignore[type-arg]

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelReviewThreadReconcileCommand(**payload)
            result = handler.handle(command)
            result_payload = result.model_dump(mode="json")
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-reconciler"
        )

        cmd_payload = json.dumps(
            {
                "thread_id": "PRRT_bus_test",
                "pr_node_id": "PR_bus_test",
                "repo": "OmniNode-ai/omniclaude",
                "pr_number": 99,
                "resolved_by": "random-human",
                "correlation_id": "bus-wire-test",
                "allowed_actors": [_ALLOWED_BOT, _BYPASS_USER],
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["reopened"] is True

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()
