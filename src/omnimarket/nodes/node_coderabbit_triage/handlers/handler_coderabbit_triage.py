# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCoderabbitTriage — CodeRabbit thread classification.

Pure deterministic handler. No LLM. Fetches CodeRabbit review threads via
the GitHub API, then classifies each thread as BLOCKING or SUGGESTION using
a hardcoded keyword list.

BLOCKING markers: action words that require a code change before merge.
SUGGESTION markers: advisory words that are safe to auto-acknowledge.

When dry_run=True, classification still runs but no replies are posted.
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

# CodeRabbit bot login name on GitHub
_CODERABBIT_BOT = "coderabbitai"


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

    Fetches PR review comments from the GitHub API, filters to CodeRabbit
    comments, and classifies each using keyword matching. No LLM.
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
        """Fetch CodeRabbit comments from GitHub API and classify them."""
        owner, name = self._split_repo(repo)
        comments = self._fetch_pr_comments(owner, name, pr_number)
        coderabbit_comments = [
            c
            for c in comments
            if (c.get("user") or {}).get("login", "").lower() == _CODERABBIT_BOT
        ]

        classifications: list[ModelThreadClassification] = []
        for comment in coderabbit_comments:
            body = comment.get("body") or ""
            severity, keyword = self.classify_body(body)
            classifications.append(
                ModelThreadClassification(
                    comment_id=comment.get("id", 0),
                    body_excerpt=body[:200],
                    severity=severity,
                    matched_keyword=keyword,
                    url=comment.get("html_url", ""),
                )
            )

        return classifications

    def _fetch_pr_comments(self, owner: str, repo: str, pr_number: int) -> list[dict]:  # type: ignore[type-arg]
        """Fetch PR review comments via gh api."""
        result = subprocess.run(
            [
                "gh",
                "api",
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
                "--paginate",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            logger.warning(
                "gh api failed for %s/%s#%d: %s",
                owner,
                repo,
                pr_number,
                result.stderr.strip(),
            )
            return []

        try:
            data = json.loads(result.stdout)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError as exc:
            logger.warning("failed to parse gh api response: %s", exc)
            return []

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
