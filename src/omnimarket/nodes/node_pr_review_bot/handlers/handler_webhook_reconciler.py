# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerWebhookReconciler — OMN-8492, Component 1.

Processes pull_request_review_thread.resolved GitHub webhook events.
If the actor that resolved the thread is not the designated review bot
(and not an authorized emergency-bypass actor), the reconciler:

1. Re-opens the thread via resolveReviewThread GraphQL mutation (reversed).
2. Posts a comment explaining that only the bot may resolve threads.
3. Emits a Kafka event: onex.evt.omnimarket.review-bot-thread-reopened.v1

This closes the CodeRabbit self-resolution loophole entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

TOPIC_THREAD_REOPENED = "onex.evt.omnimarket.review-bot-thread-reopened.v1"

logger = logging.getLogger(__name__)

_REOPEN_COMMENT = (
    "Only the onex review bot may resolve threads. "
    "Push a fix with a commit citation; the bot will re-verify."
)

_REOPEN_GRAPHQL = """
mutation ReopenPullRequestReviewThread($threadId: ID!) {
  reopenPullRequestReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


# ---------------------------------------------------------------------------
# Protocols for injected dependencies
# ---------------------------------------------------------------------------


class ProtocolGitHubGraphQL(Protocol):
    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


class ProtocolGitHubRest(Protocol):
    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> int: ...


class ProtocolKafkaPublisher(Protocol):
    def publish(self, topic: str, payload: dict[str, Any]) -> None: ...


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcilerResult:
    thread_id: str
    actor: str
    pr_number: int
    repo: str
    action_taken: str  # "reopened" | "allowed" | "bypass_allowed"
    event_id: str | None = None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerWebhookReconciler:
    """Reconciles pull_request_review_thread.resolved webhook events.

    Only the bot_login actor is permitted to resolve threads. Any other
    actor triggers an immediate reopen + comment + Kafka event.

    authorized_bypass_actors: list of GitHub handles allowed to resolve
        threads via the emergency-bypass flow. Typically empty; populated
        only after HandlerEmergencyBypassParser grants a bypass for a PR.
    """

    def __init__(
        self,
        bot_login: str,
        github_graphql: ProtocolGitHubGraphQL,
        github_rest: ProtocolGitHubRest,
        kafka_publisher: ProtocolKafkaPublisher,
        authorized_bypass_actors: list[str] | None = None,
    ) -> None:
        self._bot_login = bot_login
        self._graphql = github_graphql
        self._rest = github_rest
        self._kafka = kafka_publisher
        self._bypass_actors: frozenset[str] = frozenset(authorized_bypass_actors or [])

    def handle(
        self,
        thread_id: str,
        actor: str,
        pr_number: int,
        repo: str,
        head_sha: str,
    ) -> ReconcilerResult:
        """Process a thread-resolved webhook event.

        Returns ReconcilerResult describing what action was taken.
        """
        # Bot resolved the thread — this is the authorized path, do nothing.
        if actor == self._bot_login:
            logger.debug(
                "WebhookReconciler: bot resolved thread %s on %s PR #%d — allowed",
                thread_id,
                repo,
                pr_number,
            )
            return ReconcilerResult(
                thread_id=thread_id,
                actor=actor,
                pr_number=pr_number,
                repo=repo,
                action_taken="allowed",
            )

        # Emergency bypass actor — allow, do not reopen.
        if actor in self._bypass_actors:
            logger.info(
                "WebhookReconciler: bypass actor %s resolved thread %s on %s PR #%d — allowed",
                actor,
                thread_id,
                repo,
                pr_number,
            )
            return ReconcilerResult(
                thread_id=thread_id,
                actor=actor,
                pr_number=pr_number,
                repo=repo,
                action_taken="bypass_allowed",
            )

        # Unauthorized actor — reopen the thread.
        logger.warning(
            "WebhookReconciler: unauthorized actor %s resolved thread %s on %s PR #%d — reopening",
            actor,
            thread_id,
            repo,
            pr_number,
        )

        self._reopen_thread(thread_id)
        self._post_reopen_comment(repo, pr_number)

        event_id = str(uuid4())
        self._kafka.publish(
            TOPIC_THREAD_REOPENED,
            {
                "event_id": event_id,
                "thread_id": thread_id,
                "actor": actor,
                "pr_number": pr_number,
                "repo": repo,
                "sha": head_sha,
                "timestamp": datetime.now(tz=UTC).isoformat(),
            },
        )

        return ReconcilerResult(
            thread_id=thread_id,
            actor=actor,
            pr_number=pr_number,
            repo=repo,
            action_taken="reopened",
            event_id=event_id,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _reopen_thread(self, thread_id: str) -> None:
        try:
            self._graphql.execute(_REOPEN_GRAPHQL, {"threadId": thread_id})
        except Exception:
            logger.exception(
                "WebhookReconciler: GraphQL reopen failed for thread %s", thread_id
            )
            raise

    def _post_reopen_comment(self, repo: str, pr_number: int) -> None:
        try:
            self._rest.post_pr_comment(repo, pr_number, _REOPEN_COMMENT)
        except Exception:
            logger.exception(
                "WebhookReconciler: failed to post reopen comment on %s PR #%d",
                repo,
                pr_number,
            )
            raise


__all__: list[str] = [
    "HandlerWebhookReconciler",
    "ReconcilerResult",
]
