# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReportPoster — posts the final PR review summary as a GitHub PR comment.

Implements ``ProtocolReportPoster`` from handler_fsm.py.

Aggregates:
- Finding count by severity (CRITICAL / MAJOR / MINOR / NIT)
- Verdict (clean / risks_noted / blocking_issue)
- Thread resolution summary (pass / fail / pending)
- Human-readable summary text

Formats the summary as a markdown table + verdict badge and delegates posting
to ``AdapterGitHubBridge``. In dry_run mode the comment body is logged but
never sent to GitHub.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import Counter

from omnimarket.nodes.node_pr_review_bot.handlers.handler_fsm import (
    ProtocolReportPoster,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    EnumFindingSeverity,
    EnumPrVerdict,
    EnumThreadStatus,
    ReviewFinding,
    ReviewVerdict,
    ThreadState,
)

logger = logging.getLogger(__name__)

# Verdict badge text shown at the top of the summary comment.
_VERDICT_BADGE: dict[EnumPrVerdict, str] = {
    EnumPrVerdict.CLEAN: "PASSED",
    EnumPrVerdict.RISKS_NOTED: "RISKS NOTED",
    EnumPrVerdict.BLOCKING_ISSUE: "BLOCKED",
}

# Display order for severity rows in the findings table.
_SEVERITY_ORDER: tuple[EnumFindingSeverity, ...] = (
    EnumFindingSeverity.CRITICAL,
    EnumFindingSeverity.MAJOR,
    EnumFindingSeverity.MINOR,
    EnumFindingSeverity.NIT,
)


# ---------------------------------------------------------------------------
# GitHub bridge protocol (subset used by this handler)
# ---------------------------------------------------------------------------


class ProtocolGitHubBridge(ABC):
    """Minimal GitHub API surface required by HandlerReportPoster."""

    @abstractmethod
    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        """Post a plain PR comment (not a review thread). Raises on failure."""
        ...


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _build_findings_table(findings: tuple[ReviewFinding, ...]) -> str:
    """Return a markdown table summarising finding counts by severity."""
    counts: Counter[str] = Counter()
    for f in findings:
        counts[f.severity] += 1

    rows = []
    for sev in _SEVERITY_ORDER:
        count = counts.get(sev, 0)
        rows.append(f"| {sev.upper()} | {count} |")

    total = sum(counts.values())
    header = "| Severity | Count |\n|----------|-------|"
    body = "\n".join(rows)
    footer = f"| **Total** | **{total}** |"
    return f"{header}\n{body}\n{footer}"


def _build_thread_summary(thread_states: tuple[ThreadState, ...]) -> str:
    """Return a markdown table summarising thread resolution status."""
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

    verdict_detail = ""
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
# Handler
# ---------------------------------------------------------------------------


class HandlerReportPoster(ProtocolReportPoster):
    """Posts the final PR review summary comment via AdapterGitHubBridge.

    Implements ``ProtocolReportPoster``. Requires a ``ProtocolGitHubBridge``
    instance for the actual GitHub API call so the handler remains unit-testable
    without hitting the network.

    The ``findings`` and ``thread_states`` parameters are optional: the FSM
    ``make_verdict`` method already aggregates counts into ``ReviewVerdict``,
    but the full sequences are needed here to render the per-severity breakdown
    table. Callers must pass the current FSM state's sequences through.
    """

    def __init__(
        self,
        github_bridge: ProtocolGitHubBridge,
        findings: tuple[ReviewFinding, ...],
        thread_states: tuple[ThreadState, ...],
    ) -> None:
        self._bridge = github_bridge
        self._findings = findings
        self._thread_states = thread_states

    def post_summary(
        self,
        pr_number: int,
        repo: str,
        verdict: ReviewVerdict,
        dry_run: bool,
    ) -> None:
        """Build and post the markdown summary comment.

        In dry_run mode the body is logged at INFO level and no GitHub call
        is made. Raises on unrecoverable GitHub API failures (non-dry-run).
        """
        if verdict.total_findings and not self._findings:
            msg = "findings must be provided when verdict.total_findings > 0"
            raise ValueError(msg)

        body = build_summary_comment(verdict, self._findings, self._thread_states)

        if dry_run:
            logger.info(
                "DRY RUN — report_poster would post to %s#%d:\n%s",
                repo,
                pr_number,
                body,
            )
            return

        logger.info(
            "Posting review summary to %s#%d (verdict=%s, findings=%d)",
            repo,
            pr_number,
            verdict.verdict,
            verdict.total_findings,
        )
        self._bridge.post_pr_comment(repo=repo, pr_number=pr_number, body=body)
        logger.info("Report posted to %s#%d", repo, pr_number)


__all__: list[str] = [
    "HandlerReportPoster",
    "ProtocolGitHubBridge",
    "build_summary_comment",
]
