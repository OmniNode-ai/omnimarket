"""HandlerThreadPoster — posts ReviewFindings as GitHub PR review threads.

Implements ProtocolThreadPoster from handler_fsm.py.

Behaviour:
- MAJOR and CRITICAL findings each get their own line-level review thread.
- MINOR and NIT findings are bundled into a single summary comment (no thread spam).
- Before posting, checks for an existing bot thread for the same finding_id (R10 dedup).
- In dry_run mode, logs what would be posted but makes no API calls.
- Requires the PR head SHA from AdapterGitHubBridge to anchor review comments.
- The finding ID is embedded as an HTML comment in every thread body for dedup lookup.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from omnimarket.nodes.node_pr_review_bot.adapter_github_bridge import (
    AdapterGitHubBridge,
    ReviewThread,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_fsm import (
    ProtocolThreadPoster,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    EnumFindingSeverity,
    EnumThreadStatus,
    ReviewFinding,
    ThreadState,
)

logger = logging.getLogger(__name__)

# Bot identity marker — must match find_bot_thread_for_finding() in the bridge.
_BOT_LOGIN = "onexbot[bot]"
_FINDING_MARKER_TEMPLATE = "<!-- onexbot:finding:{finding_id} -->"

# Severities that get individual threads (all others go into the summary comment).
_THREAD_SEVERITIES: frozenset[EnumFindingSeverity] = frozenset(
    {EnumFindingSeverity.MAJOR, EnumFindingSeverity.CRITICAL}
)


def _build_thread_body(finding: ReviewFinding) -> str:
    """Render a single finding as a GitHub review thread body.

    The HTML comment marker is the machine-readable dedup key (R10).
    """
    marker = _FINDING_MARKER_TEMPLATE.format(finding_id=str(finding.id))
    suggestion_block = (
        f"\n\n**Suggested fix**: {finding.suggestion}" if finding.suggestion else ""
    )
    file_ref = ""
    if finding.evidence.file_path:
        start = finding.evidence.line_start or "?"
        end = finding.evidence.line_end or start
        file_ref = (
            f"\n\n\U0001f4cd File: `{finding.evidence.file_path}`, lines {start}-{end}"
        )

    return (
        f"{marker}\n"
        f"**[PR-BOT] Finding: {finding.title}**"
        f" (severity: {finding.severity}, confidence: {finding.confidence})\n\n"
        f"{finding.description}"
        f"{file_ref}"
        f"{suggestion_block}\n\n"
        "**Resolution required before merge.** This thread will be verified by the "
        "judge model before it can be dismissed. Post a reply explaining the fix and "
        "tag `@onexbot-judge verify`."
    )


def _build_summary_body(minor_findings: list[ReviewFinding]) -> str:
    """Bundle MINOR/NIT findings into a single summary comment."""
    lines = ["**[PR-BOT] Review Notes** (informational — not blocking merge)\n"]
    for f in minor_findings:
        severity_tag = f.severity.upper()
        location = ""
        if f.evidence.file_path:
            start = f.evidence.line_start or "?"
            location = f" (`{f.evidence.file_path}:{start}`)"
        lines.append(f"- **[{severity_tag}] {f.title}**{location}: {f.description}")
    return "\n".join(lines)


class HandlerThreadPoster(ProtocolThreadPoster):
    """Posts ReviewFindings as GitHub PR review threads via AdapterGitHubBridge.

    Sync wrapper — run_sync() drives the underlying async methods so the FSM
    (which calls post() synchronously) does not need to be async-aware.
    """

    def __init__(
        self,
        bridge: AdapterGitHubBridge | None = None,
        *,
        max_findings_per_pr: int = 20,
        bot_login: str = _BOT_LOGIN,
    ) -> None:
        self._bridge = bridge
        self._max_findings_per_pr = max_findings_per_pr
        self._bot_login = bot_login

    # ------------------------------------------------------------------
    # ProtocolThreadPoster implementation
    # ------------------------------------------------------------------

    def post(
        self,
        pr_number: int,
        repo: str,
        findings: tuple[ReviewFinding, ...],
        dry_run: bool,
    ) -> list[ThreadState]:
        """Post threads for MAJOR/CRITICAL findings; bundle MINOR/NIT into summary.

        Returns a list of ThreadState — one per finding that warranted a thread.
        Findings below threshold are not represented individually in ThreadState.
        """
        return asyncio.get_event_loop().run_until_complete(
            self._post_async(pr_number, repo, findings, dry_run)
        )

    # ------------------------------------------------------------------
    # Async implementation
    # ------------------------------------------------------------------

    async def _post_async(
        self,
        pr_number: int,
        repo: str,
        findings: tuple[ReviewFinding, ...],
        dry_run: bool,
    ) -> list[ThreadState]:
        if self._bridge is None:
            raise RuntimeError(
                "HandlerThreadPoster: bridge is not configured. "
                "Pass an AdapterGitHubBridge instance or configure via DI container."
            )

        thread_findings = [f for f in findings if f.severity in _THREAD_SEVERITIES]
        minor_findings = [f for f in findings if f.severity not in _THREAD_SEVERITIES]

        # Cap thread count to prevent spam (design doc max_findings_per_pr).
        if len(thread_findings) > self._max_findings_per_pr:
            logger.warning(
                "Capping thread findings from %d to %d for PR #%d in %s",
                len(thread_findings),
                self._max_findings_per_pr,
                pr_number,
                repo,
            )
            thread_findings = thread_findings[: self._max_findings_per_pr]

        # Fetch head SHA once — needed to anchor review comments.
        # Degrade gracefully: if metadata fetch fails, fall back to posting
        # all findings as general PR comments (no line-level anchoring).
        head_sha = ""
        if not dry_run and thread_findings:
            try:
                pr_meta = await self._bridge.fetch_pr_metadata(repo, pr_number)
                head_sha = pr_meta.head_sha
            except Exception:
                logger.exception(
                    "Failed to fetch PR metadata for PR #%d in %s — "
                    "findings will be posted as general comments without line anchors",
                    pr_number,
                    repo,
                )

        # Fetch all existing bot review threads once for R10 dedup. This
        # prevents re-paginating GitHub on every finding in the loop.
        existing_threads: list[ReviewThread] = []
        if not dry_run and thread_findings:
            try:
                existing_threads = await self._bridge.fetch_review_threads(
                    repo, pr_number
                )
            except Exception:
                logger.exception(
                    "Failed to fetch existing review threads for PR #%d in %s — "
                    "dedup check will be skipped; findings may be posted twice",
                    pr_number,
                    repo,
                )

        thread_states: list[ThreadState] = []

        for finding in thread_findings:
            state = await self._post_finding_thread(
                pr_number=pr_number,
                repo=repo,
                finding=finding,
                head_sha=head_sha,
                dry_run=dry_run,
                cached_threads=existing_threads,
            )
            thread_states.append(state)

        # Post bundled summary for non-thread findings.
        if minor_findings:
            if dry_run:
                logger.info(
                    "[dry_run] Would post summary comment for %d minor/nit findings "
                    "on PR #%d in %s",
                    len(minor_findings),
                    pr_number,
                    repo,
                )
            else:
                summary_body = _build_summary_body(minor_findings)
                try:
                    await self._bridge.post_pr_comment(repo, pr_number, summary_body)
                    logger.info(
                        "Posted summary comment for %d minor/nit findings on PR #%d in %s",
                        len(minor_findings),
                        pr_number,
                        repo,
                    )
                except Exception:
                    logger.exception(
                        "Failed to post summary comment for PR #%d in %s",
                        pr_number,
                        repo,
                    )

        return thread_states

    async def _post_finding_thread(
        self,
        *,
        pr_number: int,
        repo: str,
        finding: ReviewFinding,
        head_sha: str,
        dry_run: bool,
        cached_threads: list[ReviewThread],
    ) -> ThreadState:
        """Post (or skip) a single finding thread. Returns the resulting ThreadState.

        Uses cached_threads for R10 dedup to avoid re-paginating GitHub per finding.
        """
        assert self._bridge is not None  # guarded by _post_async caller
        finding_id_str = str(finding.id)
        marker = _FINDING_MARKER_TEMPLATE.format(finding_id=finding_id_str)

        # R10 dedup: check cached threads instead of re-fetching from GitHub.
        if not dry_run:
            existing = next(
                (
                    t
                    for t in cached_threads
                    if t.user_login == self._bot_login and marker in t.body
                ),
                None,
            )
            if existing is not None:
                logger.info(
                    "Skipping finding %s — bot thread %d already exists on PR #%d",
                    finding_id_str,
                    existing.id,
                    pr_number,
                )
                return ThreadState(
                    finding_id=finding.id,
                    github_thread_id=existing.id,
                    status=EnumThreadStatus.POSTED,
                    posted_at=datetime.now(tz=UTC),
                )

        body = _build_thread_body(finding)

        if dry_run:
            logger.info(
                "[dry_run] Would post thread for finding %s (severity=%s) on PR #%d in %s",
                finding_id_str,
                finding.severity,
                pr_number,
                repo,
            )
            return ThreadState(
                finding_id=finding.id,
                github_thread_id=None,
                status=EnumThreadStatus.PENDING,
                posted_at=None,
            )

        # Determine file and line for the review comment anchor.
        file_path = finding.evidence.file_path or ""
        line_start = finding.evidence.line_start

        if not file_path or line_start is None:
            # No file/line context — fall back to a general PR comment.
            logger.warning(
                "Finding %s has no file_path or line_start; posting as general PR comment",
                finding_id_str,
            )
            try:
                comment_id = await self._bridge.post_pr_comment(repo, pr_number, body)
                logger.info(
                    "Posted general comment (id=%d) for finding %s on PR #%d in %s",
                    comment_id,
                    finding_id_str,
                    pr_number,
                    repo,
                )
                return ThreadState(
                    finding_id=finding.id,
                    github_thread_id=comment_id,
                    status=EnumThreadStatus.POSTED,
                    posted_at=datetime.now(tz=UTC),
                )
            except Exception:
                logger.exception(
                    "Failed to post general comment for finding %s on PR #%d",
                    finding_id_str,
                    pr_number,
                )
                return ThreadState(
                    finding_id=finding.id,
                    github_thread_id=None,
                    status=EnumThreadStatus.PENDING,
                )

        try:
            thread = await self._bridge.post_review_comment(
                repo=repo,
                pr_number=pr_number,
                commit_id=head_sha,
                path=file_path,
                line=line_start,
                body=body,
            )
            logger.info(
                "Posted review thread (id=%d) for finding %s (severity=%s) on PR #%d in %s",
                thread.id,
                finding_id_str,
                finding.severity,
                pr_number,
                repo,
            )
            return ThreadState(
                finding_id=finding.id,
                github_thread_id=thread.id,
                status=EnumThreadStatus.POSTED,
                posted_at=datetime.now(tz=UTC),
            )
        except Exception:
            logger.exception(
                "Failed to post review thread for finding %s on PR #%d in %s",
                finding_id_str,
                pr_number,
                repo,
            )
            return ThreadState(
                finding_id=finding.id,
                github_thread_id=None,
                status=EnumThreadStatus.PENDING,
            )


__all__: list[str] = ["HandlerThreadPoster"]
