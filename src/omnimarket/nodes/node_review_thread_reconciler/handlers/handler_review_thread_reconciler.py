# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReviewThreadReconciler — re-opens PR threads resolved by non-bot actors.

Listens on onex.cmd.omnimarket.review-thread-reconcile.v1.
For each event, checks resolved_by against the configurable allowed_actors list.
If the resolver is not in the list, calls GitHub GraphQL to unresolve the thread,
posts a policy reminder comment, and emits onex.evt.omnimarket.review-thread-reopened.v1.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_review_thread_reconciler.protocols.protocol_github_client import (
    ProtocolGitHubReviewClient,
)

TOPIC_EVT_THREAD_REOPENED = "onex.evt.omnimarket.review-thread-reopened.v1"

if TYPE_CHECKING:
    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

logger = logging.getLogger(__name__)

_POLICY_REMINDER = (
    "Only the onex review bot may resolve threads. "
    "Push a fix with a commit citation; the bot will re-verify."
)


class ModelReviewThreadReconcileCommand(BaseModel):
    """Input command payload from onex.cmd.omnimarket.review-thread-reconcile.v1."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    pr_node_id: str
    repo: str
    pr_number: int
    resolved_by: str
    correlation_id: str
    allowed_actors: list[str] = Field(default_factory=list)


class ModelReviewThreadReconcileResult(BaseModel):
    """Result returned by HandlerReviewThreadReconciler."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    repo: str
    pr_number: int
    resolved_by: str
    reopened: bool
    correlation_id: str
    processed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class HandlerReviewThreadReconciler:
    """Reconciler that re-opens threads resolved by non-bot actors.

    Dependencies are injected so the handler remains testable without
    hitting GitHub or a real Kafka broker.
    """

    def __init__(
        self,
        github_client: ProtocolGitHubReviewClient | None = None,
        event_bus: EventBusInmemory | None = None,
    ) -> None:
        self._client = github_client
        self._event_bus = event_bus

    def handle(
        self, command: ModelReviewThreadReconcileCommand
    ) -> ModelReviewThreadReconcileResult:
        """Primary handler entry point."""
        if self._client is None:
            raise RuntimeError(
                "HandlerReviewThreadReconciler: github_client is not configured. "
                "Pass a ProtocolGitHubReviewClient instance or configure via DI container."
            )

        allowed_lower = {actor.lower() for actor in command.allowed_actors}
        resolver_lower = command.resolved_by.lower()

        if resolver_lower in allowed_lower:
            return ModelReviewThreadReconcileResult(
                thread_id=command.thread_id,
                repo=command.repo,
                pr_number=command.pr_number,
                resolved_by=command.resolved_by,
                reopened=False,
                correlation_id=command.correlation_id,
            )

        # Non-allowed actor — re-open the thread and post policy reminder.
        # Failures propagate to the caller; retry/DLQ is the consumer's responsibility.
        try:
            self._client.unresolve_thread(command.thread_id)
            logger.debug(
                "reconciler: unresolve_thread succeeded for %s", command.thread_id
            )
        except Exception:
            logger.exception(
                "reconciler: failed to unresolve thread %s on %s#%d",
                command.thread_id,
                command.repo,
                command.pr_number,
            )
            raise
        try:
            self._client.post_comment(command.pr_node_id, _POLICY_REMINDER)
            logger.debug(
                "reconciler: post_comment succeeded on %s#%d",
                command.repo,
                command.pr_number,
            )
        except Exception:
            logger.exception(
                "reconciler: failed to post policy comment on %s#%d",
                command.repo,
                command.pr_number,
            )
            raise

        result = ModelReviewThreadReconcileResult(
            thread_id=command.thread_id,
            repo=command.repo,
            pr_number=command.pr_number,
            resolved_by=command.resolved_by,
            reopened=True,
            correlation_id=command.correlation_id,
        )

        # Callers in async context should await emit_event(command, result) after handle().
        # handle() is sync so it can be called from both sync and async code.

        logger.info(
            "reconciler: re-opened thread %s (resolved by %s) on %s#%d",
            command.thread_id,
            command.resolved_by,
            command.repo,
            command.pr_number,
        )
        return result

    async def emit_event(
        self,
        command: ModelReviewThreadReconcileCommand,
        result: ModelReviewThreadReconcileResult,
    ) -> None:
        """Publish thread_reopened event. Must be awaited by the async caller.

        Keeping this async allows callers (Kafka consumers and tests) to await
        completion directly, eliminating the event-loop scheduling complexity.
        Failures are logged but do not fail the reconcile operation.
        """
        payload = {
            "thread_id": command.thread_id,
            "pr_node_id": command.pr_node_id,
            "repo": command.repo,
            "pr_number": command.pr_number,
            "reopened_by": command.resolved_by,
            "correlation_id": command.correlation_id,
            "processed_at": result.processed_at.isoformat(),
        }
        try:
            await self._event_bus.publish(  # type: ignore[union-attr]
                TOPIC_EVT_THREAD_REOPENED, key=None, value=json.dumps(payload).encode()
            )
        except Exception:
            logger.exception("failed to emit %s event", TOPIC_EVT_THREAD_REOPENED)


__all__: list[str] = [
    "HandlerReviewThreadReconciler",
    "ModelReviewThreadReconcileCommand",
    "ModelReviewThreadReconcileResult",
]
