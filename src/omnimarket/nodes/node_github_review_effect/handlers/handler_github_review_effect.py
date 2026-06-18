# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_github_review_effect (OMN-13212 / B2).

EFFECT node. Single GitHub review-side I/O boundary absorbing the deleted
node_pr_review_bot GitHub handlers (adapter_github_bridge + thread poster +
thread watcher + report poster). Performs exactly one of three operations per
dispatch — post_threads, watch_threads, post_report — and emits the resulting
thread states (or posted-report id) as a result event over the bus.

The GitHub token is resolved at ``handle()`` time from the contract-declared
``api_key_ref`` (``GITHUB_TOKEN``) via the canonical secret-store resolver — no
direct ``os.environ`` read and no subprocess shell-out. The async HTTP bridge is
composed internally (the I/O boundary of an EFFECT node).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_github_review_effect.github_bridge import (
    AdapterGithubReviewBridge,
    ReviewThread,
)
from omnimarket.review.pr_review_io import (
    EnumFindingSeverity,
    EnumPrVerdict,
    EnumThreadStatus,
    ReviewFinding,
    ReviewVerdict,
    ThreadState,
)
from omnimarket.review.pr_review_node_io import (
    EnumGithubReviewOperation,
    ModelGithubReviewCommand,
    ModelGithubReviewResultEvent,
)

_log = logging.getLogger(__name__)
_HANDLER_ID = "node_github_review_effect"
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

_BOT_LOGIN = "onexbot[bot]"
_FINDING_MARKER_TEMPLATE = "<!-- onexbot:finding:{finding_id} -->"
_VERIFY_TRIGGER = "@onexbot-judge verify"
_MAX_VERIFY_ATTEMPTS = 3

_THREAD_SEVERITIES: frozenset[EnumFindingSeverity] = frozenset(
    {EnumFindingSeverity.MAJOR, EnumFindingSeverity.CRITICAL}
)
_VERDICT_BADGE: dict[EnumPrVerdict, str] = {
    EnumPrVerdict.CLEAN: "PASSED",
    EnumPrVerdict.RISKS_NOTED: "RISKS NOTED",
    EnumPrVerdict.BLOCKING_ISSUE: "BLOCKED",
}
_SEVERITY_ORDER: tuple[EnumFindingSeverity, ...] = (
    EnumFindingSeverity.CRITICAL,
    EnumFindingSeverity.MAJOR,
    EnumFindingSeverity.MINOR,
    EnumFindingSeverity.NIT,
)


# ---------------------------------------------------------------------------
# Markdown builders (pure)
# ---------------------------------------------------------------------------


def _build_thread_body(finding: ReviewFinding) -> str:
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
    lines = ["**[PR-BOT] Review Notes** (informational — not blocking merge)\n"]
    for f in minor_findings:
        severity_tag = f.severity.upper()
        location = ""
        if f.evidence.file_path:
            start = f.evidence.line_start or "?"
            location = f" (`{f.evidence.file_path}:{start}`)"
        lines.append(f"- **[{severity_tag}] {f.title}**{location}: {f.description}")
    return "\n".join(lines)


def _build_findings_table(findings: tuple[ReviewFinding, ...]) -> str:
    counts: Counter[str] = Counter()
    for f in findings:
        counts[f.severity] += 1
    rows = [f"| {sev.upper()} | {counts.get(sev, 0)} |" for sev in _SEVERITY_ORDER]
    total = sum(counts.values())
    header = "| Severity | Count |\n|----------|-------|"
    return f"{header}\n" + "\n".join(rows) + f"\n| **Total** | **{total}** |"


def _build_thread_summary(thread_states: tuple[ThreadState, ...]) -> str:
    pass_count = sum(
        1 for t in thread_states if t.status == EnumThreadStatus.VERIFIED_PASS
    )
    fail_count = sum(
        1 for t in thread_states if t.status == EnumThreadStatus.VERIFIED_FAIL
    )
    pending_count = sum(
        1
        for t in thread_states
        if t.status in (EnumThreadStatus.PENDING, EnumThreadStatus.POSTED)
    )
    resolved_count = sum(
        1 for t in thread_states if t.status == EnumThreadStatus.RESOLVED
    )
    escalated_count = sum(
        1 for t in thread_states if t.status == EnumThreadStatus.ESCALATED
    )
    lines = [
        "| Status | Count |",
        "|--------|-------|",
        f"| Verified PASS | {pass_count} |",
        f"| Verified FAIL | {fail_count} |",
        f"| Pending / Unresolved | {pending_count} |",
        f"| Resolved (awaiting verification) | {resolved_count} |",
        f"| Escalated | {escalated_count} |",
    ]
    return "\n".join(lines)


def build_summary_comment(
    verdict: ReviewVerdict,
    findings: tuple[ReviewFinding, ...],
    thread_states: tuple[ThreadState, ...],
) -> str:
    """Render the full markdown summary comment body."""
    badge = _VERDICT_BADGE.get(verdict.verdict, verdict.verdict.upper())
    duration_s = verdict.duration_ms / 1000.0
    header = f"## PR Review Bot — {badge}"
    meta = (
        f"**Run ID**: `{verdict.correlation_id}`  \n"
        f"**Judge model**: `{verdict.judge_model_used}`  \n"
        f"**Duration**: {duration_s:.1f}s"
    )
    findings_section = "### Findings by Severity\n\n" + _build_findings_table(findings)
    summary_section = f"### Summary\n\n{verdict.summary}" if verdict.summary else ""
    threads_section = (
        "### Thread Resolution Summary\n\n" + _build_thread_summary(thread_states)
        if thread_states
        else ""
    )
    if verdict.verdict == EnumPrVerdict.BLOCKING_ISSUE:
        verdict_detail = (
            "> **Merge is blocked.** One or more MAJOR/CRITICAL findings were not "
            "verified as resolved by the judge model. Address each failing thread "
            "and tag `@onexbot-judge verify` to request re-verification."
        )
    elif verdict.verdict == EnumPrVerdict.RISKS_NOTED:
        verdict_detail = (
            "> Findings were noted but all required threads passed verification. "
            "Review the findings table for NITs and MINORs if desired."
        )
    else:
        verdict_detail = "> No blocking findings. This PR is clear to merge."
    sections = [header, meta, verdict_detail, summary_section, findings_section]
    if threads_section:
        sections.append(threads_section)
    return "\n\n".join(s for s in sections if s)


# ---------------------------------------------------------------------------
# EFFECT handler
# ---------------------------------------------------------------------------


class HandlerGithubReviewEffect:
    """EFFECT: perform one GitHub review-side I/O operation per dispatch.

    Args:
        bridge: Optional concrete bridge. When omitted, a bridge is built at
            ``handle()`` time with the token resolved from the contract
            ``api_key_ref``. Injecting a bridge keeps tests deterministic
            (no network, no secret store).
    """

    def __init__(self, bridge: AdapterGithubReviewBridge | None = None) -> None:
        self._bridge = bridge

    async def handle(
        self, command: ModelGithubReviewCommand
    ) -> ModelHandlerOutput[None]:
        """Dispatch to the requested GitHub operation and emit a result event."""
        bridge = await self._resolve_bridge(command.dry_run)

        if command.operation is EnumGithubReviewOperation.POST_THREADS:
            thread_states = await self._post_threads(bridge, command)
            report_comment_id = None
        elif command.operation is EnumGithubReviewOperation.WATCH_THREADS:
            thread_states = await self._watch_threads(bridge, command)
            report_comment_id = None
        else:  # POST_REPORT
            thread_states = list(command.thread_states)
            report_comment_id = await self._post_report(bridge, command)

        event = ModelGithubReviewResultEvent(
            correlation_id=command.correlation_id,
            operation=command.operation,
            repo=command.repo,
            pr_number=command.pr_number,
            thread_states=tuple(thread_states),
            report_comment_id=report_comment_id,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=command.correlation_id,
            handler_id=_HANDLER_ID,
            events=(event,),
        )

    async def _resolve_bridge(self, dry_run: bool) -> AdapterGithubReviewBridge | None:
        if self._bridge is not None:
            return self._bridge
        if dry_run:
            # dry_run performs no GitHub writes/reads; a bridge is not needed.
            return None
        github_ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
        secret = await resolve_api_key_async(github_ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {github_ref!r} resolved to None — "
                "ensure GITHUB_TOKEN is set in the secret store."
            )
        return AdapterGithubReviewBridge(token=secret.get_secret_value())

    # ------------------------------------------------------------------
    # post_threads
    # ------------------------------------------------------------------

    async def _post_threads(
        self,
        bridge: AdapterGithubReviewBridge | None,
        command: ModelGithubReviewCommand,
    ) -> list[ThreadState]:
        repo, pr_number = command.repo, command.pr_number
        thread_findings = [
            f for f in command.findings if f.severity in _THREAD_SEVERITIES
        ]
        minor_findings = [
            f for f in command.findings if f.severity not in _THREAD_SEVERITIES
        ]

        if len(thread_findings) > command.max_findings_per_pr:
            _log.warning(
                "Capping thread findings from %d to %d for PR #%d in %s",
                len(thread_findings),
                command.max_findings_per_pr,
                pr_number,
                repo,
            )
            thread_findings = thread_findings[: command.max_findings_per_pr]

        if command.dry_run:
            return [
                ThreadState(
                    finding_id=f.id,
                    github_thread_id=None,
                    status=EnumThreadStatus.PENDING,
                )
                for f in thread_findings
            ]

        assert bridge is not None  # non-dry-run resolves a bridge

        head_sha = ""
        if thread_findings:
            try:
                pr_meta = await bridge.fetch_pr_metadata(repo, pr_number)
                head_sha = pr_meta.head_sha
            except Exception:
                _log.exception(
                    "Failed to fetch PR metadata for PR #%d in %s — findings will "
                    "be posted as general comments without line anchors",
                    pr_number,
                    repo,
                )

        existing_threads: list[ReviewThread] = []
        if thread_findings:
            try:
                existing_threads = await bridge.fetch_review_threads(repo, pr_number)
            except Exception:
                _log.exception(
                    "Failed to fetch existing review threads for PR #%d in %s — "
                    "dedup check skipped",
                    pr_number,
                    repo,
                )

        thread_states: list[ThreadState] = []
        for finding in thread_findings:
            thread_states.append(
                await self._post_finding_thread(
                    bridge, repo, pr_number, finding, head_sha, existing_threads
                )
            )

        if minor_findings:
            try:
                await bridge.post_pr_comment(
                    repo, pr_number, _build_summary_body(minor_findings)
                )
            except Exception:
                _log.exception(
                    "Failed to post summary comment for PR #%d in %s", pr_number, repo
                )

        return thread_states

    async def _post_finding_thread(
        self,
        bridge: AdapterGithubReviewBridge,
        repo: str,
        pr_number: int,
        finding: ReviewFinding,
        head_sha: str,
        cached_threads: list[ReviewThread],
    ) -> ThreadState:
        finding_id_str = str(finding.id)
        marker = _FINDING_MARKER_TEMPLATE.format(finding_id=finding_id_str)

        existing = next(
            (
                t
                for t in cached_threads
                if t.user_login == _BOT_LOGIN and marker in t.body
            ),
            None,
        )
        if existing is not None:
            return ThreadState(
                finding_id=finding.id,
                github_thread_id=existing.id,
                status=EnumThreadStatus.POSTED,
                posted_at=datetime.now(tz=UTC),
            )

        body = _build_thread_body(finding)
        file_path = finding.evidence.file_path or ""
        line_start = finding.evidence.line_start

        if not file_path or line_start is None or not head_sha:
            try:
                comment_id = await bridge.post_pr_comment(repo, pr_number, body)
                return ThreadState(
                    finding_id=finding.id,
                    github_thread_id=comment_id,
                    status=EnumThreadStatus.POSTED,
                    posted_at=datetime.now(tz=UTC),
                )
            except Exception:
                _log.exception(
                    "Failed to post general comment for finding %s on PR #%d",
                    finding_id_str,
                    pr_number,
                )
                return ThreadState(
                    finding_id=finding.id, status=EnumThreadStatus.PENDING
                )

        try:
            thread = await bridge.post_review_comment(
                repo=repo,
                pr_number=pr_number,
                commit_id=head_sha,
                path=file_path,
                line=line_start,
                body=body,
            )
            return ThreadState(
                finding_id=finding.id,
                github_thread_id=thread.id,
                status=EnumThreadStatus.POSTED,
                posted_at=datetime.now(tz=UTC),
            )
        except Exception:
            _log.exception(
                "Failed to post review thread for finding %s on PR #%d in %s",
                finding_id_str,
                pr_number,
                repo,
            )
            return ThreadState(finding_id=finding.id, status=EnumThreadStatus.PENDING)

    # ------------------------------------------------------------------
    # watch_threads (single poll pass — orchestrator drives the loop)
    # ------------------------------------------------------------------

    async def _watch_threads(
        self,
        bridge: AdapterGithubReviewBridge | None,
        command: ModelGithubReviewCommand,
    ) -> list[ThreadState]:
        states = [t.model_copy(deep=True) for t in command.thread_states]
        watchable = [
            t
            for t in states
            if t.status == EnumThreadStatus.POSTED and t.github_thread_id is not None
        ]
        if not watchable or command.dry_run:
            return states

        assert bridge is not None
        all_threads = await bridge.fetch_review_threads(command.repo, command.pr_number)
        thread_map = {t.id: t for t in all_threads}

        for state in states:
            if (
                state.status != EnumThreadStatus.POSTED
                or state.github_thread_id is None
            ):
                continue
            root = thread_map.get(state.github_thread_id)
            if root is None:
                continue
            self._evaluate_thread(state, root, all_threads)
        return states

    @staticmethod
    def _evaluate_thread(
        state: ThreadState,
        root_thread: ReviewThread,
        all_threads: list[ReviewThread],
    ) -> None:
        assert state.github_thread_id is not None
        thread_comments = [
            t
            for t in all_threads
            if t.id == state.github_thread_id
            or t.in_reply_to_id == state.github_thread_id
        ]
        verify_requests = [
            c
            for c in thread_comments
            if _VERIFY_TRIGGER in c.body and c.user_login != _BOT_LOGIN
        ]
        if not root_thread.resolved and not verify_requests:
            return

        if state.verify_attempts >= _MAX_VERIFY_ATTEMPTS:
            state.status = EnumThreadStatus.ESCALATED
            state.resolved_at = datetime.now(tz=UTC)
            return

        reply_bodies = [c.body for c in thread_comments if c.user_login != _BOT_LOGIN]
        state.status = EnumThreadStatus.RESOLVED
        state.resolved_at = datetime.now(tz=UTC)
        state.judge_reasoning = "\n---\n".join(reply_bodies) if reply_bodies else None

    # ------------------------------------------------------------------
    # post_report
    # ------------------------------------------------------------------

    async def _post_report(
        self,
        bridge: AdapterGithubReviewBridge | None,
        command: ModelGithubReviewCommand,
    ) -> int | None:
        if command.verdict is None:
            raise ValueError("POST_REPORT requires a verdict")
        body = build_summary_comment(
            command.verdict, command.findings, command.thread_states
        )
        if command.dry_run:
            _log.info(
                "[dry_run] Would post review summary to %s#%d (verdict=%s)",
                command.repo,
                command.pr_number,
                command.verdict.verdict,
            )
            return None
        assert bridge is not None
        return await bridge.post_pr_comment(command.repo, command.pr_number, body)


__all__: list[str] = ["HandlerGithubReviewEffect", "build_summary_comment"]
