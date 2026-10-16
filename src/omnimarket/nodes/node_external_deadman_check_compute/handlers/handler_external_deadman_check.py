# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge the two watchdog legs' mirrors from outside the lab (OMN-20808, plan 4.8).

The external dead-man raises in exactly three situations, each read from the mirror variables
the legs write every five minutes:

1. Both mirrors are stale (older than the staleness bound) or unreadable.
2. A fresh mirror reports an episode undelivered or unrecorded past its delivery-due time plus the
   escalation bound.
3. A fresh mirror reports the day's canary episode past its receipt deadline and lacking its
   delivered or its recorded event.

One stale mirror beside a fresh one is not an alarm here: each leg watches the other on the bus,
and this check is the route for the day both go quiet or the alarm path itself breaks. The check
is pure; the workflow supplies the variables, the open issues and the clock, and acts on the
result.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from pydantic import ValidationError

from omnimarket.nodes.node_external_deadman_check_compute.models import (
    ISSUE_TITLE,
    EnumExternalDeadmanAction,
    EnumExternalDeadmanReason,
    ModelExternalDeadmanBacklog,
    ModelExternalDeadmanCheckRequest,
    ModelExternalDeadmanCheckResult,
    ModelExternalDeadmanIssue,
    ModelExternalDeadmanMirror,
    ModelExternalDeadmanReasonFinding,
)

_CODES_MARKER = re.compile(r"<!--\s*external-deadman:\s*codes=([a-z_,]*)\s*-->")


def _read_mirror(
    raw: str | None, leg: str
) -> tuple[ModelExternalDeadmanMirror | None, str]:
    """The parsed mirror, or ``None`` with the reason it cannot be used."""
    if raw is None or not raw.strip():
        return None, "variable missing or empty"
    try:
        mirror = ModelExternalDeadmanMirror.model_validate_json(raw)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "document"
        return None, f"unreadable at {where}: {first['type']}"
    if mirror.leg != leg:
        return None, f"variable names leg {mirror.leg}, expected {leg}"
    return mirror, ""


def _escalation_overdue(
    backlog: ModelExternalDeadmanBacklog, now: datetime, bound: timedelta
) -> bool:
    return (
        backlog.count > 0
        and backlog.oldest_due_at is not None
        and now > backlog.oldest_due_at + bound
    )


def _fresh_findings(
    mirror: ModelExternalDeadmanMirror, now: datetime, bound: timedelta
) -> list[ModelExternalDeadmanReasonFinding]:
    findings: list[ModelExternalDeadmanReasonFinding] = []
    if _escalation_overdue(mirror.undelivered, now, bound):
        findings.append(
            ModelExternalDeadmanReasonFinding(
                code=EnumExternalDeadmanReason.EPISODE_UNDELIVERED,
                leg=mirror.leg,
                detail=(
                    f"{mirror.undelivered.count} episode(s) not delivered, oldest due "
                    f"{mirror.undelivered.oldest_due_at:%Y-%m-%dT%H:%M:%SZ}"
                ),
            )
        )
    if _escalation_overdue(mirror.unrecorded, now, bound):
        findings.append(
            ModelExternalDeadmanReasonFinding(
                code=EnumExternalDeadmanReason.EPISODE_UNRECORDED,
                leg=mirror.leg,
                detail=(
                    f"{mirror.unrecorded.count} episode(s) not recorded, oldest due "
                    f"{mirror.unrecorded.oldest_due_at:%Y-%m-%dT%H:%M:%SZ}"
                ),
            )
        )
    if mirror.canary is not None:
        missing = mirror.canary.missing(now)
        if missing:
            findings.append(
                ModelExternalDeadmanReasonFinding(
                    code=EnumExternalDeadmanReason.CANARY_INCOMPLETE,
                    leg=mirror.leg,
                    detail=(
                        f"canary injected {mirror.canary.injection_at:%Y-%m-%dT%H:%M:%SZ}, "
                        f"receipt deadline {mirror.canary.receipt_deadline:%Y-%m-%dT%H:%M:%SZ}"
                        f" passed; lacks: {', '.join(missing)}"
                    ),
                )
            )
    return findings


def _existing_issue(
    issues: tuple[ModelExternalDeadmanIssue, ...],
) -> ModelExternalDeadmanIssue | None:
    ours = [issue for issue in issues if issue.title == ISSUE_TITLE]
    return min(ours, key=lambda issue: issue.number) if ours else None


def _codes_in(body: str) -> frozenset[str]:
    found = _CODES_MARKER.search(body)
    if found is None:
        return frozenset()
    return frozenset(code for code in found.group(1).split(",") if code)


def _issue_body(
    findings: list[ModelExternalDeadmanReasonFinding], now: datetime, codes: list[str]
) -> str:
    lines = [
        f"<!-- external-deadman: codes={','.join(codes)} -->",
        "",
        "The external dead-man, a scheduled workflow on a GitHub-hosted runner, raised. "
        "Each run refreshes this issue; it is never opened twice. Close it once the cause is gone.",
        "",
        f"Checked at {now:%Y-%m-%dT%H:%M:%SZ}.",
        "",
    ]
    lines.extend(f"- `{f.code.value}` ({f.leg}): {f.detail}" for f in findings)
    return "\n".join(lines) + "\n"


class HandlerExternalDeadmanCheck:
    """handle(request) -> result for the external dead-man (OMN-20808)."""

    def handle(
        self, request: ModelExternalDeadmanCheckRequest
    ) -> ModelExternalDeadmanCheckResult:
        now = request.now
        stale_after = timedelta(seconds=request.mirror_stale_after_seconds)
        bound = timedelta(seconds=request.escalation_bound_seconds)

        fresh: list[ModelExternalDeadmanMirror] = []
        stale_notes: list[str] = []
        for leg, raw in (
            ("primary", request.primary_mirror),
            ("deadman", request.deadman_mirror),
        ):
            mirror, why = _read_mirror(raw, leg)
            if mirror is None:
                stale_notes.append(f"{leg}: {why}")
            elif now - mirror.written_at > stale_after:
                stale_notes.append(
                    f"{leg}: last written {mirror.written_at:%Y-%m-%dT%H:%M:%SZ}"
                )
            else:
                fresh.append(mirror)

        findings: list[ModelExternalDeadmanReasonFinding] = []
        if not fresh:
            findings.append(
                ModelExternalDeadmanReasonFinding(
                    code=EnumExternalDeadmanReason.BOTH_MIRRORS_STALE,
                    leg="both",
                    detail=(
                        f"no mirror newer than {request.mirror_stale_after_seconds} seconds; "
                        + "; ".join(stale_notes)
                    ),
                )
            )
        for mirror in fresh:
            findings.extend(_fresh_findings(mirror, now, bound))

        if not findings:
            return ModelExternalDeadmanCheckResult(
                alarm=False,
                reasons=(),
                action=EnumExternalDeadmanAction.NONE,
                issue_number=None,
                issue_title=ISSUE_TITLE,
                issue_body="",
                notify=False,
                slack_text="",
            )

        codes = sorted({finding.code.value for finding in findings})
        body = _issue_body(findings, now, codes)
        existing = _existing_issue(request.open_issues)
        if existing is None:
            action = EnumExternalDeadmanAction.OPEN_ISSUE
            notify = True
        else:
            action = EnumExternalDeadmanAction.UPDATE_ISSUE
            notify = frozenset(codes) != _codes_in(existing.body)
        return ModelExternalDeadmanCheckResult(
            alarm=True,
            reasons=tuple(findings),
            action=action,
            issue_number=existing.number if existing is not None else None,
            issue_title=ISSUE_TITLE,
            issue_body=body,
            notify=notify,
            slack_text=f"{ISSUE_TITLE}: {', '.join(codes)}",
        )
