# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCommitCitationVerifier — OMN-8492, Component 2.

Bot verification loop extension: after every new commit on an open PR,
parse commit messages for citations like:
  - "Fixes <thread_id>"
  - "Addressed in commit <sha>"
  - "Resolves <thread_id>"

For each cited thread, runs hostile_reviewer against the diff scoped to
the finding. If verification passes, the bot resolves the thread via
GraphQL. If it fails, it posts a reply explaining why.

Also emits:
  - onex.evt.omnimarket.review-bot-thread-resolved.v1 on bot resolution
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from omnimarket.nodes.node_pr_review_bot.topics import TOPIC_THREAD_RESOLVED

logger = logging.getLogger(__name__)

# Patterns recognized as commit citations referencing a thread
_CITATION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bFixes\s+([A-Za-z0-9_\-]+)\b", re.IGNORECASE),
    re.compile(r"\bResolves\s+([A-Za-z0-9_\-]+)\b", re.IGNORECASE),
    re.compile(r"\bAddressed in commit\s+([0-9a-f]{5,40})\b", re.IGNORECASE),
    re.compile(r"\bCloses\s+([A-Za-z0-9_\-]+)\b", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class ProtocolHostileReviewer(Protocol):
    """Run adversarial review on a diff snippet for a specific thread."""

    def verify(self, diff: str, thread_id: str, finding_description: str) -> bool:
        """Return True if the diff adequately addresses the finding."""
        ...


class ProtocolGitHubGraphQL(Protocol):
    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


class ProtocolGitHubRest(Protocol):
    def post_thread_reply(
        self, repo: str, pr_number: int, thread_id: str, body: str
    ) -> None: ...

    def post_commit_status(
        self,
        repo: str,
        sha: str,
        state: str,
        context: str,
        description: str,
    ) -> None: ...


class ProtocolKafkaPublisher(Protocol):
    def publish(self, topic: str, payload: dict[str, Any]) -> None: ...


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

_RESOLVE_GRAPHQL = """
mutation ResolvePullRequestReviewThread($threadId: ID!) {
  resolvePullRequestReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""

STATUS_CONTEXT = "review-bot/all-findings-resolved"


@dataclass
class ThreadCitation:
    thread_id: str
    sha: str
    commit_message: str


@dataclass
class CitationVerifyResult:
    thread_id: str
    resolved: bool
    reason: str
    event_id: str | None = None


@dataclass
class CommitVerificationResult:
    pr_number: int
    repo: str
    sha: str
    citations_found: list[ThreadCitation] = field(default_factory=list)
    thread_results: list[CitationVerifyResult] = field(default_factory=list)
    all_resolved: bool = False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerCommitCitationVerifier:
    """Parses commit messages for thread citations and verifies them.

    After each new commit on an open PR, this handler:
    1. Parses commit messages for citation patterns.
    2. For each cited thread, runs hostile_reviewer on the scoped diff.
    3. Bot resolves threads that pass; posts failure reason for those that fail.
    4. Updates the GitHub commit status review-bot/all-findings-resolved.
    5. Emits Kafka events for resolved threads.
    """

    def __init__(
        self,
        bot_login: str,
        hostile_reviewer: ProtocolHostileReviewer,
        github_graphql: ProtocolGitHubGraphQL,
        github_rest: ProtocolGitHubRest,
        kafka_publisher: ProtocolKafkaPublisher,
    ) -> None:
        self._bot_login = bot_login
        self._reviewer = hostile_reviewer
        self._graphql = github_graphql
        self._rest = github_rest
        self._kafka = kafka_publisher

    def process_commit(
        self,
        pr_number: int,
        repo: str,
        sha: str,
        commit_messages: list[str],
        open_thread_ids: list[str],
        thread_findings: dict[str, str],
        diff_by_thread: dict[str, str],
    ) -> CommitVerificationResult:
        """Process a new commit push on an open PR.

        Args:
            pr_number: GitHub PR number.
            repo: Repository in owner/repo format.
            sha: Head commit SHA after the push.
            commit_messages: All new commit messages since last push.
            open_thread_ids: Thread IDs currently open on the PR.
            thread_findings: Map of thread_id -> finding description.
            diff_by_thread: Map of thread_id -> relevant diff snippet.

        Returns:
            CommitVerificationResult with per-thread verdicts.
        """
        result = CommitVerificationResult(pr_number=pr_number, repo=repo, sha=sha)

        # If there are no open threads at all, we are already resolved.
        if not open_thread_ids:
            result.all_resolved = True
            self._update_commit_status(repo, sha, [])
            return result

        citations = self._parse_citations(commit_messages, open_thread_ids, sha)
        result.citations_found = citations

        if not citations:
            logger.debug(
                "CommitCitationVerifier: no citations found in %d commit(s) for %s PR #%d",
                len(commit_messages),
                repo,
                pr_number,
            )
            self._update_commit_status(repo, sha, open_thread_ids)
            return result

        for citation in citations:
            finding_desc = thread_findings.get(citation.thread_id, "")
            diff = diff_by_thread.get(citation.thread_id, "")
            verify_result = self._verify_and_act(
                pr_number, repo, sha, citation, finding_desc, diff
            )
            result.thread_results.append(verify_result)

        # Compute remaining open threads
        resolved_ids = {r.thread_id for r in result.thread_results if r.resolved}
        remaining_open = [t for t in open_thread_ids if t not in resolved_ids]
        result.all_resolved = len(remaining_open) == 0

        self._update_commit_status(repo, sha, remaining_open)
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_citations(
        self,
        commit_messages: list[str],
        open_thread_ids: list[str],
        sha: str,
    ) -> list[ThreadCitation]:
        """Extract thread citations from commit messages."""
        open_set = set(open_thread_ids)
        seen: set[str] = set()
        citations: list[ThreadCitation] = []

        for msg in commit_messages:
            for pattern in _CITATION_PATTERNS:
                for match in pattern.finditer(msg):
                    ref = match.group(1)
                    if ref in open_set and ref not in seen:
                        citations.append(
                            ThreadCitation(thread_id=ref, sha=sha, commit_message=msg)
                        )
                        seen.add(ref)

        return citations

    def _verify_and_act(
        self,
        pr_number: int,
        repo: str,
        sha: str,
        citation: ThreadCitation,
        finding_desc: str,
        diff: str,
    ) -> CitationVerifyResult:
        """Run hostile_reviewer on the diff; resolve or report failure."""
        try:
            passed = self._reviewer.verify(
                diff=diff,
                thread_id=citation.thread_id,
                finding_description=finding_desc,
            )
        except Exception:
            logger.exception(
                "CommitCitationVerifier: hostile_reviewer raised for thread %s",
                citation.thread_id,
            )
            passed = False

        if passed:
            return self._resolve_thread(pr_number, repo, sha, citation)
        self._post_failure_reply(pr_number, repo, citation, finding_desc)
        return CitationVerifyResult(
            thread_id=citation.thread_id,
            resolved=False,
            reason="hostile_reviewer did not verify the fix",
        )

    def _resolve_thread(
        self,
        pr_number: int,
        repo: str,
        sha: str,
        citation: ThreadCitation,
    ) -> CitationVerifyResult:
        """Bot resolves the thread via GraphQL and emits Kafka event."""
        try:
            self._graphql.execute(_RESOLVE_GRAPHQL, {"threadId": citation.thread_id})
        except Exception:
            logger.exception(
                "CommitCitationVerifier: GraphQL resolve failed for thread %s",
                citation.thread_id,
            )
            return CitationVerifyResult(
                thread_id=citation.thread_id,
                resolved=False,
                reason="GraphQL resolve mutation failed",
            )

        event_id = str(uuid4())
        self._kafka.publish(
            TOPIC_THREAD_RESOLVED,
            {
                "event_id": event_id,
                "thread_id": citation.thread_id,
                "pr_number": pr_number,
                "repo": repo,
                "sha": sha,
                "resolved_by": self._bot_login,
                "timestamp": datetime.now(tz=UTC).isoformat(),
            },
        )
        logger.info(
            "CommitCitationVerifier: resolved thread %s on %s PR #%d (sha=%s)",
            citation.thread_id,
            repo,
            pr_number,
            sha,
        )
        return CitationVerifyResult(
            thread_id=citation.thread_id,
            resolved=True,
            reason="hostile_reviewer verified the fix",
            event_id=event_id,
        )

    def _post_failure_reply(
        self,
        pr_number: int,
        repo: str,
        citation: ThreadCitation,
        finding_desc: str,
    ) -> None:
        body = (
            f"The fix in commit `{citation.sha[:8]}` was reviewed but "
            f"does not fully address this finding.\n\n"
            f"**Finding**: {finding_desc}\n\n"
            "Push an updated fix and include `Fixes <thread_id>` in your commit message."
        )
        try:
            self._rest.post_thread_reply(repo, pr_number, citation.thread_id, body)
        except Exception:
            logger.exception(
                "CommitCitationVerifier: failed to post failure reply for thread %s",
                citation.thread_id,
            )

    def _update_commit_status(
        self,
        repo: str,
        sha: str,
        remaining_open_threads: list[str],
    ) -> None:
        """Update the review-bot/all-findings-resolved commit status."""
        if not remaining_open_threads:
            state = "success"
            description = "All review findings resolved by review bot"
        else:
            state = "failure"
            description = (
                f"{len(remaining_open_threads)} unresolved finding(s) — "
                "push a fix and cite the thread ID"
            )
        try:
            self._rest.post_commit_status(
                repo,
                sha,
                state,
                STATUS_CONTEXT,
                description,
            )
        except Exception:
            logger.exception(
                "CommitCitationVerifier: failed to update commit status for %s@%s",
                repo,
                sha,
            )


__all__: list[str] = [
    "STATUS_CONTEXT",
    "CitationVerifyResult",
    "CommitVerificationResult",
    "HandlerCommitCitationVerifier",
    "ThreadCitation",
]
