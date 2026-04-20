# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCoderabbitTriage — CodeRabbit thread classification + auto-resolve.

Deterministic handler. No LLM. Fetches CodeRabbit review threads via the
GitHub GraphQL API (reviewThreads endpoint), classifies each thread as
BLOCKING or SUGGESTION using a hardcoded keyword list, and for SUGGESTION
threads posts an acknowledgment reply + resolves the thread via the GitHub
GraphQL resolveReviewThread mutation.

BLOCKING markers: action words that require a code change before merge —
never auto-resolved; require human action.
SUGGESTION markers: advisory words that are safe to auto-acknowledge and
close.

When dry_run=True, classification still runs but no GitHub API calls are
made. In wet mode (dry_run=False) each unresolved SUGGESTION thread produces
exactly one reply POST and one resolveReviewThread mutation; already-resolved
threads and BLOCKING threads are skipped.

NOTE: Uses GraphQL reviewThreads (not REST /pulls/{pr}/comments). The REST
endpoint only returns inline diff comments, missing review threads that
CodeRabbit posts as formal review thread objects — the common case.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_BLOCKING_KEYWORDS: frozenset[str] = frozenset(
    {
        "critical",
        "blocker",
        "blocking",
        "must fix",
        "must be fixed",
        "must change",
        "must update",
        "security",
        "vulnerability",
        "cve",
        "data loss",
        "regression",
        "breaking change",
        "important",
        "major",
        "required",
        "action required",
        "needs to be",
        "should be fixed",
        "incorrect",
        "wrong",
        "bug",
        "error",
    }
)

_SUGGESTION_KEYWORDS: frozenset[str] = frozenset(
    {
        "nitpick",
        "nit:",
        "nit ",
        "suggestion",
        "minor",
        "style",
        "cosmetic",
        "optional",
        "consider",
        "could",
        "might",
        "prefer",
        "aesthetic",
        "readability",
        "formatting",
        "whitespace",
        "typo",
    }
)

_CODERABBIT_BOT_LOGINS: frozenset[str] = frozenset(
    {"coderabbitai", "coderabbitai[bot]"}
)

_ACK_BODY_SUGGESTION = (
    "Acknowledged — triaged as SUGGESTION by node_coderabbit_triage. "
    "Tracked in tech-debt; resolving thread to unblock merge."
)

_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          comments(first: 5) {
            nodes {
              databaseId
              author {
                login
              }
              body
              path
              url
            }
          }
        }
      }
    }
  }
}
"""

_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread {
      id
      isResolved
    }
  }
}
"""


class EnumThreadSeverity(StrEnum):
    """Classification result for a CodeRabbit thread."""

    BLOCKING = "BLOCKING"
    SUGGESTION = "SUGGESTION"
    UNKNOWN = "UNKNOWN"


@runtime_checkable
class ProtocolGhApi(Protocol):
    """GitHub API seam so handler behavior is testable without the gh CLI."""

    def reply_to_thread(
        self,
        *,
        repo: str,
        pull_number: int,
        comment_id: int,
        body: str,
    ) -> None: ...

    def resolve_review_thread(self, *, thread_id: str) -> None: ...


class GhApiSubprocess:
    """Concrete ProtocolGhApi impl that shells out to `gh`.

    REST reply: `POST /repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies`
    GraphQL resolve: `resolveReviewThread(threadId: ID!)` mutation.
    """

    def reply_to_thread(
        self,
        *,
        repo: str,
        pull_number: int,
        comment_id: int,
        body: str,
    ) -> None:
        endpoint = f"/repos/{repo}/pulls/{pull_number}/comments/{comment_id}/replies"
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                endpoint,
                "-f",
                f"body={body}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"gh api POST {endpoint} failed: {result.stderr.strip()}"
            )

    def resolve_review_thread(self, *, thread_id: str) -> None:
        result = subprocess.run(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={_RESOLVE_THREAD_MUTATION}",
                "-F",
                f"threadId={thread_id}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"gh api graphql resolveReviewThread failed for {thread_id}: "
                f"{result.stderr.strip()}"
            )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"failed to parse resolveReviewThread response for {thread_id}"
            ) from exc
        errors = data.get("errors")
        if errors:
            raise RuntimeError(
                f"resolveReviewThread GraphQL errors for {thread_id}: {errors}"
            )


class ModelCoderabbitTriageCommand(BaseModel):
    """Input command for CodeRabbit triage handler."""

    model_config = ConfigDict(extra="forbid")

    repo: str
    pr_number: int
    correlation_id: str
    dry_run: bool = False


class ModelThreadClassification(BaseModel):
    """Classification result for a single CodeRabbit thread."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    comment_id: int
    body_excerpt: str
    severity: EnumThreadSeverity
    matched_keyword: str = ""
    url: str = ""
    thread_id: str = ""
    is_resolved: bool = False
    acted: bool = False


class ModelCoderabbitTriageResult(BaseModel):
    """Result emitted by HandlerCoderabbitTriage."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    repo: str
    pr_number: int
    total_threads: int = 0
    blocking_count: int = 0
    suggestion_count: int = 0
    unknown_count: int = 0
    resolved_count: int = 0
    threads: list[ModelThreadClassification] = Field(default_factory=list)
    dry_run: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def has_blockers(self) -> bool:
        return self.blocking_count > 0


class HandlerCoderabbitTriage:
    """CodeRabbit thread classification + auto-resolve handler.

    Fetches PR review threads from the GitHub GraphQL API, filters to
    CodeRabbit threads, classifies each using keyword matching, and for
    unresolved SUGGESTION threads posts an acknowledgment reply and resolves
    the thread via the resolveReviewThread GraphQL mutation. BLOCKING threads
    are never auto-resolved.

    Uses GraphQL reviewThreads instead of REST /pulls/{pr}/comments because
    CodeRabbit posts findings as review thread objects, not inline diff
    comments.
    """

    def __init__(self, gh_api: ProtocolGhApi | None = None) -> None:
        self._gh_api: ProtocolGhApi = (
            gh_api if gh_api is not None else GhApiSubprocess()
        )

    def handle(
        self, command: ModelCoderabbitTriageCommand
    ) -> ModelCoderabbitTriageResult:
        """Primary handler protocol entry point."""
        started_at = datetime.now(tz=UTC)

        threads = self._fetch_and_classify(command.repo, command.pr_number)

        updated_threads: list[ModelThreadClassification] = []
        resolved_count = 0
        for thread in threads:
            should_act = (
                thread.severity == EnumThreadSeverity.SUGGESTION
                and not thread.is_resolved
                and thread.thread_id != ""
            )
            if should_act and not command.dry_run:
                try:
                    self._gh_api.reply_to_thread(
                        repo=command.repo,
                        pull_number=command.pr_number,
                        comment_id=thread.comment_id,
                        body=_ACK_BODY_SUGGESTION,
                    )
                    self._gh_api.resolve_review_thread(thread_id=thread.thread_id)
                except Exception:
                    logger.exception(
                        "coderabbit_triage auto-resolve failed for thread %s",
                        thread.thread_id,
                    )
                    updated_threads.append(thread)
                    continue
                updated_threads.append(thread.model_copy(update={"acted": True}))
                resolved_count += 1
            else:
                if should_act and command.dry_run:
                    logger.info(
                        "coderabbit_triage dry_run: would reply+resolve thread %s "
                        "(comment_id=%s)",
                        thread.thread_id,
                        thread.comment_id,
                    )
                updated_threads.append(thread)

        blocking = [
            t for t in updated_threads if t.severity == EnumThreadSeverity.BLOCKING
        ]
        suggestion = [
            t for t in updated_threads if t.severity == EnumThreadSeverity.SUGGESTION
        ]
        unknown = [
            t for t in updated_threads if t.severity == EnumThreadSeverity.UNKNOWN
        ]

        return ModelCoderabbitTriageResult(
            correlation_id=command.correlation_id,
            repo=command.repo,
            pr_number=command.pr_number,
            total_threads=len(updated_threads),
            blocking_count=len(blocking),
            suggestion_count=len(suggestion),
            unknown_count=len(unknown),
            resolved_count=resolved_count,
            threads=updated_threads,
            dry_run=command.dry_run,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
        )

    def classify_body(self, body: str) -> tuple[EnumThreadSeverity, str]:
        """Classify a single comment body. Returns (severity, matched_keyword).

        First checks BLOCKING keywords — if any match, returns BLOCKING.
        Then checks SUGGESTION keywords — if any match, returns SUGGESTION.
        Otherwise returns UNKNOWN.
        """
        lower = body.lower()

        for keyword in sorted(_BLOCKING_KEYWORDS):
            if keyword in lower:
                return EnumThreadSeverity.BLOCKING, keyword

        for keyword in sorted(_SUGGESTION_KEYWORDS):
            if keyword in lower:
                return EnumThreadSeverity.SUGGESTION, keyword

        return EnumThreadSeverity.UNKNOWN, ""

    def _fetch_and_classify(
        self, repo: str, pr_number: int
    ) -> list[ModelThreadClassification]:
        """Fetch CodeRabbit review threads via GraphQL and classify them."""
        owner, name = self._split_repo(repo)
        raw_threads = self._fetch_review_threads(owner, name, pr_number)

        classifications: list[ModelThreadClassification] = []
        for thread in raw_threads:
            comments = (thread.get("comments") or {}).get("nodes") or []
            if not comments:
                continue
            first_comment = comments[0]
            author_login = (first_comment.get("author") or {}).get("login", "")
            if author_login.lower() not in _CODERABBIT_BOT_LOGINS:
                continue

            body = first_comment.get("body") or ""
            severity, keyword = self.classify_body(body)
            classifications.append(
                ModelThreadClassification(
                    comment_id=first_comment.get("databaseId") or 0,
                    body_excerpt=body[:200],
                    severity=severity,
                    matched_keyword=keyword,
                    url=first_comment.get("url", ""),
                    thread_id=thread.get("id") or "",
                    is_resolved=bool(thread.get("isResolved", False)),
                )
            )

        return classifications

    def _fetch_review_threads(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict]:  # type: ignore[type-arg]
        """Fetch PR review threads via GitHub GraphQL API with pagination."""
        all_threads: list[dict] = []  # type: ignore[type-arg]
        cursor: str | None = None

        while True:
            variables: dict[str, object] = {
                "owner": owner,
                "repo": repo,
                "pr": pr_number,
            }
            if cursor is not None:
                variables["cursor"] = cursor

            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    f"query={_REVIEW_THREADS_QUERY}",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"repo={repo}",
                    "-F",
                    f"pr={pr_number}",
                ]
                + (["-F", f"cursor={cursor}"] if cursor is not None else []),
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                msg = (
                    f"gh api graphql failed for {owner}/{repo}#{pr_number}: "
                    f"{result.stderr.strip()}"
                )
                raise RuntimeError(msg)

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"failed to parse gh graphql response for {owner}/{repo}#{pr_number}"
                ) from exc

            errors = data.get("errors")
            if errors:
                raise RuntimeError(
                    f"GraphQL errors for {owner}/{repo}#{pr_number}: {errors}"
                )

            review_threads = (
                (data.get("data") or {})
                .get("repository", {})
                .get("pullRequest", {})
                .get("reviewThreads", {})
            )
            if not review_threads:
                raise RuntimeError(
                    f"missing reviewThreads in GraphQL response for "
                    f"{owner}/{repo}#{pr_number}"
                )

            nodes = review_threads.get("nodes") or []
            all_threads.extend(nodes)

            page_info = review_threads.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break

        return all_threads

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        """Split 'owner/repo' into (owner, repo). Raises ValueError if invalid."""
        parts = repo.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            msg = f"Invalid repo format: {repo!r}. Expected 'owner/repo'."
            raise ValueError(msg)
        return parts[0], parts[1]


__all__: list[str] = [
    "EnumThreadSeverity",
    "GhApiSubprocess",
    "HandlerCoderabbitTriage",
    "ModelCoderabbitTriageCommand",
    "ModelCoderabbitTriageResult",
    "ModelThreadClassification",
    "ProtocolGhApi",
]
