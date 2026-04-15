# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verification loop — commit-citation parsing + hostile_reviewer integration.

On every push to an open PR:
1. Parse commit message bodies for citations (Fixes <id>, Addressed in commit <sha>,
   Resolves thread <id>, Resolves: #findings[N]).
2. For each cited finding, extract the scoped diff hunk and dispatch hostile_reviewer.
3. If BOTH configured models return CLEAN → call resolveReviewThread mutation.
4. If any model returns DIRTY/FAIL, or models disagree → post a rejection reply.

A finding with no explicit commit citation is NEVER resolved — no implicit matching.

OMN-8494
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from omnimarket.nodes.node_pr_review_bot.models.models import (
    DiffHunk,
    ReviewFinding,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Citation patterns — order matters, more specific first
# ---------------------------------------------------------------------------

_FIXES_PATTERN = re.compile(
    r"(?i)\bfixes\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)
_RESOLVES_THREAD_PATTERN = re.compile(
    r"(?i)\bresolves\s+thread\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)


@dataclass(frozen=True)
class CommitCitation:
    """A single citation extracted from a commit message body."""

    thread_id: str | None = None


def parse_commit_citations(commit_body: str) -> list[CommitCitation]:
    """Extract thread-id citations from a commit message body.

    Recognized formats:
    - ``Fixes <uuid>``
    - ``Resolves thread <uuid>``

    Prose acknowledgments ("will fix", "acknowledged", task-tracker prefixes) are never matched.
    False positives are acceptable — hostile_reviewer is the gate, not the parser.
    """
    citations: list[CommitCitation] = []

    for m in _FIXES_PATTERN.finditer(commit_body):
        citations.append(CommitCitation(thread_id=m.group(1)))

    for m in _RESOLVES_THREAD_PATTERN.finditer(commit_body):
        citations.append(CommitCitation(thread_id=m.group(1)))

    return citations


# ---------------------------------------------------------------------------
# Injected protocols
# ---------------------------------------------------------------------------


class ProtocolHostileReviewerInvoker(ABC):
    """Invokes hostile_reviewer scoped to a diff hunk for a specific finding."""

    @abstractmethod
    def review_hunk(
        self,
        *,
        model_key: str,
        diff_content: str,
        finding_context: str,
    ) -> dict[str, str]:
        """Return dict with keys 'verdict' (CLEAN|DIRTY) and 'reasoning'."""
        ...


class ProtocolGraphQLClient(ABC):
    """GitHub GraphQL client — abstracts resolveReviewThread and reply mutations."""

    @abstractmethod
    def resolve_thread(
        self,
        *,
        pr_number: int,
        repo: str,
        thread_id: str,
    ) -> bool:
        """Call the resolveReviewThread mutation. Returns True on success."""
        ...

    @abstractmethod
    def post_thread_reply(
        self,
        *,
        pr_number: int,
        repo: str,
        thread_id: str,
        body: str,
    ) -> None:
        """Post a reply comment on the given review thread."""
        ...


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    resolved_thread_ids: list[str] = field(default_factory=list)
    rejected_thread_ids: list[str] = field(default_factory=list)
    skipped_thread_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerVerificationLoop:
    """Runs the verification loop for a single push event.

    For each open finding:
    - If the commit body cites the finding ID → scope the diff hunk, dispatch
      hostile_reviewer on ALL configured models, and resolve only if ALL agree CLEAN.
    - If no citation → skip silently (no comment, thread stays open).
    - If any model returns DIRTY or models disagree → post a rejection reply.

    All I/O is via injected protocols (ProtocolHostileReviewerInvoker and
    ProtocolGraphQLClient) so tests can mock both without network access.
    """

    def __init__(
        self,
        reviewer: ProtocolHostileReviewerInvoker,
        graphql_client: ProtocolGraphQLClient,
        reviewer_models: list[str],
    ) -> None:
        if not reviewer_models:
            raise ValueError(
                "reviewer_models must not be empty — no reviewers configured means "
                "no verification is possible; fail closed rather than silently pass."
            )
        self._reviewer = reviewer
        self._gql = graphql_client
        self._reviewer_models = reviewer_models

    def run(
        self,
        *,
        commit_body: str,
        commit_sha: str,
        pr_number: int,
        repo: str,
        open_findings: list[ReviewFinding],
        diff_hunks: list[DiffHunk],
    ) -> VerificationResult:
        """Process all open findings against the new commit."""
        citations = parse_commit_citations(commit_body)
        cited_thread_ids: set[str] = {
            c.thread_id for c in citations if c.thread_id is not None
        }

        result = VerificationResult()

        for finding in open_findings:
            fid = str(finding.id)

            if fid not in cited_thread_ids:
                logger.debug(
                    "Finding %s not cited in commit %s — skipping", fid, commit_sha
                )
                result.skipped_thread_ids.append(fid)
                continue

            scoped_hunk = self._scope_hunk(finding, diff_hunks)
            if scoped_hunk is None:
                logger.info(
                    "No matching diff hunk for finding %s in commit %s — leaving thread open",
                    fid,
                    commit_sha,
                )
                result.rejected_thread_ids.append(fid)
                continue
            diff_content = scoped_hunk.content
            finding_context = (
                f"Finding: {finding.title}\n"
                f"Severity: {finding.severity}\n"
                f"Description: {finding.description}\n"
            )
            if finding.suggestion:
                finding_context += f"Suggested fix: {finding.suggestion}\n"

            verdicts: list[tuple[str, str]] = []
            for model_key in self._reviewer_models:
                try:
                    response = self._reviewer.review_hunk(
                        model_key=model_key,
                        diff_content=diff_content,
                        finding_context=finding_context,
                    )
                    verdicts.append(
                        (
                            response.get("verdict", "DIRTY"),
                            response.get("reasoning", ""),
                        )
                    )
                except Exception as exc:
                    logger.exception(
                        "hostile_reviewer failed for model %s finding %s: %s",
                        model_key,
                        fid,
                        exc,
                    )
                    verdicts.append(("DIRTY", f"Reviewer error: {exc}"))

            all_clean = all(v.upper() == "CLEAN" for v, _ in verdicts)

            if all_clean:
                logger.info("All models CLEAN for finding %s — resolving thread", fid)
                try:
                    resolved = self._gql.resolve_thread(
                        pr_number=pr_number,
                        repo=repo,
                        thread_id=fid,
                    )
                    if resolved:
                        result.resolved_thread_ids.append(fid)
                    else:
                        logger.warning(
                            "resolveReviewThread returned False for %s — mutation failed silently",
                            fid,
                        )
                        result.rejected_thread_ids.append(fid)
                except Exception as exc:
                    logger.exception("resolveReviewThread failed for %s: %s", fid, exc)
                    result.rejected_thread_ids.append(fid)
            else:
                dirty_reasons = "; ".join(
                    reasoning
                    for verdict, reasoning in verdicts
                    if verdict.upper() != "CLEAN"
                )
                reply_body = (
                    f"Verification failed: {dirty_reasons or 'models did not converge on CLEAN'}. "
                    "Thread remains open."
                )
                logger.info(
                    "Non-CLEAN verdict for finding %s — posting rejection reply", fid
                )
                try:
                    self._gql.post_thread_reply(
                        pr_number=pr_number,
                        repo=repo,
                        thread_id=fid,
                        body=reply_body,
                    )
                except Exception as exc:
                    logger.exception("post_thread_reply failed for %s: %s", fid, exc)
                result.rejected_thread_ids.append(fid)

        return result

    @staticmethod
    def _scope_hunk(
        finding: ReviewFinding,
        diff_hunks: list[DiffHunk],
    ) -> DiffHunk | None:
        """Return the best-matching diff hunk for the finding's file+line range."""
        fp = finding.evidence.file_path
        line_start = finding.evidence.line_start
        line_end = finding.evidence.line_end

        if fp is None:
            return None

        for hunk in diff_hunks:
            if hunk.file_path != fp:
                continue
            if line_start is None:
                return hunk
            if hunk.start_line <= line_start and hunk.end_line >= (
                line_end or line_start
            ):
                return hunk

        # Fallback: any hunk for the same file
        for hunk in diff_hunks:
            if hunk.file_path == fp:
                return hunk

        return None


__all__: list[str] = [
    "CommitCitation",
    "HandlerVerificationLoop",
    "ProtocolGraphQLClient",
    "ProtocolHostileReviewerInvoker",
    "VerificationResult",
    "parse_commit_citations",
]
