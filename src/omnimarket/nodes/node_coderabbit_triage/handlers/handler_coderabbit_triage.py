# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCoderabbitTriage — CodeRabbit thread classification.

Pure deterministic handler. No LLM. Fetches CodeRabbit review threads via
the GitHub GraphQL API (reviewThreads endpoint), then classifies each thread
as BLOCKING or SUGGESTION using a hardcoded keyword list.

BLOCKING markers: action words that require a code change before merge.
SUGGESTION markers: advisory words that are safe to auto-acknowledge.

When dry_run=True, classification still runs but no replies are posted.

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

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Keywords that indicate a thread requires a code fix (BLOCKING).
# Checked case-insensitively against the thread body.
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

# Keywords that indicate a thread is advisory only (SUGGESTION).
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

# Supported CodeRabbit bot login names on GitHub (exact match only)
_CODERABBIT_BOT_LOGINS: frozenset[str] = frozenset(
    {"coderabbitai", "coderabbitai[bot]"}
)

# GraphQL query to fetch review threads for a PR
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


class EnumThreadSeverity(StrEnum):
    """Classification result for a CodeRabbit thread."""

    BLOCKING = "BLOCKING"
    SUGGESTION = "SUGGESTION"
    UNKNOWN = "UNKNOWN"


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
    body_excerpt: str  # First 200 chars of the thread body
    severity: EnumThreadSeverity
    matched_keyword: str = ""
    url: str = ""


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
    threads: list[ModelThreadClassification] = Field(default_factory=list)
    dry_run: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def has_blockers(self) -> bool:
        return self.blocking_count > 0


class HandlerCoderabbitTriage:
    """CodeRabbit thread classification handler.

    Fetches PR review threads from the GitHub GraphQL API, filters to
    CodeRabbit threads, and classifies each using keyword matching. No LLM.

    Uses GraphQL reviewThreads instead of REST /pulls/{pr}/comments because
    CodeRabbit posts findings as review thread objects, not inline diff
    comments. The REST endpoint returns total_threads=0 for those PRs.
    """

    def handle(
        self, command: ModelCoderabbitTriageCommand
    ) -> ModelCoderabbitTriageResult:
        """Primary handler protocol entry point."""
        started_at = datetime.now(tz=UTC)

        threads = self._fetch_and_classify(command.repo, command.pr_number)

        blocking = [t for t in threads if t.severity == EnumThreadSeverity.BLOCKING]
        suggestion = [t for t in threads if t.severity == EnumThreadSeverity.SUGGESTION]
        unknown = [t for t in threads if t.severity == EnumThreadSeverity.UNKNOWN]

        return ModelCoderabbitTriageResult(
            correlation_id=command.correlation_id,
            repo=command.repo,
            pr_number=command.pr_number,
            total_threads=len(threads),
            blocking_count=len(blocking),
            suggestion_count=len(suggestion),
            unknown_count=len(unknown),
            threads=threads,
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
            # First comment in thread is the root — filter by author
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
    "HandlerCoderabbitTriage",
    "ModelCoderabbitTriageCommand",
    "ModelCoderabbitTriageResult",
    "ModelThreadClassification",
]
