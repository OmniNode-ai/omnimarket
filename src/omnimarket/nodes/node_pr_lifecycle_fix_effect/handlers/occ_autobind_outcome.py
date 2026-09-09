# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable, product-PR-visible outcome for every consumed occ-autobind command.

Why this module exists (OMN-18069)
----------------------------------
On 2026-09-09 the effects runtime consumed 37 consecutive
``onex.cmd.omnimarket.occ-autobind.v1`` commands across 17 product PRs in 6
repos and minted **zero** companions. Every one of them was delivered, consumed
and handled: the handler caught the exception, logged one WARNING into a
container log nobody watches, and published a result to a topic whose name ends
``-fix-completed``. The publishing CI job was green and had finished minutes
earlier. Nothing on any product PR said a word.

The failure was not undetected. It was **untold**: already typed, already on the
bus, and addressed to nobody.

What this module changes
------------------------
Every consumed autobind command now ends in a check-run on the product PR's own
head SHA -- the one surface a human and the merge gate both already read:

* ``ERROR``    -> ``conclusion: failure``. An infrastructure, credential or
  transport fault. The companion will NOT appear without intervention, so this
  is red, and it also gets a PR comment (a check-run alone is a line in a
  rollup; a comment is a notification).
* ``DECLINED`` -> ``conclusion: neutral``. A policy decision the emitter made on
  purpose (lease held, mergeability suppression, already bound, dry run). It
  must be legible, but it must never newly block a merge.
* ``MINTED``   -> ``conclusion: success``. Recorded so a *missing* outcome is
  distinguishable from a passing one: a check surface that only ever appears on
  failure cannot tell "it worked" from "nobody ran it", and telling those apart
  is the whole job.

The summary carries a machine-readable first line, :data:`OUTCOME_MARKER_PREFIX`,
so ``check_occ_companion_merged`` in the product repos can read a terminal
verdict instead of polling its 1500-second deadline and then reporting only that
a deadline passed.

Reporting is strictly best-effort and never raises into the caller: a fix run
that already failed must not be turned into a *second*, different failure by its
own reporter. Every reporting fault is logged and swallowed -- the bus terminal
remains the authoritative record.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from uuid import UUID

from omnimarket.github_api import GitHubApiError, rest_json, split_repo

logger = logging.getLogger(__name__)

# The check-run name product repos read. Stable: `check_occ_companion_merged`
# matches on it exactly, so renaming it is a cross-repo change.
AUTOBIND_OUTCOME_CHECK_NAME = "occ-autobind / outcome"

# First line of the check-run summary. Machine-readable on purpose.
OUTCOME_MARKER_PREFIX = "occ-autobind-outcome:"

# Marker that makes the PR comment idempotent per correlation id, so a replayed
# or retried command does not spam the product PR with duplicates.
_COMMENT_MARKER = "<!-- occ-autobind-outcome -->"


class EnumAutobindOutcome(StrEnum):
    """Terminal disposition of one consumed occ-autobind command."""

    MINTED = "MINTED"
    DECLINED = "DECLINED"
    ERROR = "ERROR"


_CONCLUSION_BY_OUTCOME: dict[EnumAutobindOutcome, str] = {
    EnumAutobindOutcome.MINTED: "success",
    EnumAutobindOutcome.DECLINED: "neutral",
    EnumAutobindOutcome.ERROR: "failure",
}


def render_outcome_summary(
    *,
    outcome: EnumAutobindOutcome,
    reason: str,
    repo: str,
    pr_number: int,
    correlation_id: UUID | str | None,
) -> str:
    """Render the check-run summary, machine-readable first line first.

    The reason is collapsed onto one line so the marker line is parseable with a
    line-oriented read; the full detail follows underneath for a human.
    """
    flat_reason = " ".join(str(reason).split()) or "(no reason given)"
    marker = (
        f"{OUTCOME_MARKER_PREFIX} {outcome.value} "
        f"repo={repo} pr={pr_number} "
        f"correlation_id={correlation_id or 'unknown'} "
        f"reason={flat_reason}"
    )
    return (
        f"{marker}\n\n"
        f"The occ-autobind command for {repo}#{pr_number} was delivered, consumed "
        f"and handled by the effects runtime. Its terminal disposition was "
        f"**{outcome.value}**.\n\n"
        f"Reason: {flat_reason}\n\n"
        f"An `ERROR` outcome means the OCC evidence companion will NOT appear "
        f"without intervention -- do not wait on it. A `DECLINED` outcome is a "
        f"deliberate policy decision and never blocks a merge (OMN-18069)."
    )


def _render_comment(
    *,
    outcome: EnumAutobindOutcome,
    reason: str,
    repo: str,
    pr_number: int,
    correlation_id: UUID | str | None,
) -> str:
    flat_reason = " ".join(str(reason).split()) or "(no reason given)"
    return (
        f"{_COMMENT_MARKER}\n"
        f"### OCC autobind did not mint a companion for this PR\n\n"
        f"The `occ-autobind` command for `{repo}#{pr_number}` was published, "
        f"delivered and consumed by the effects runtime, and then **failed**. "
        f"No OCC evidence companion was created, and none will appear on its "
        f"own.\n\n"
        f"- Outcome: `{outcome.value}`\n"
        f"- Reason: `{flat_reason}`\n"
        f"- Correlation id: `{correlation_id or 'unknown'}`\n\n"
        f"Hand-author the companion by union-resolving this PR's item onto the "
        f"ticket's existing contract (leaving prior entries byte-identical), or "
        f"repair the runtime fault named above and re-run the publisher. "
        f"(OMN-18069)"
    )


def _resolve_head_sha(*, repo: str, pr_number: int, token: str) -> str | None:
    owner, repo_name = split_repo(repo)
    data = rest_json(
        "GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}", token=token
    )
    head = data.get("head")
    if not isinstance(head, dict):
        return None
    sha = head.get("sha")
    return sha if isinstance(sha, str) and sha else None


def _comment_already_posted(
    *, repo: str, pr_number: int, token: str, correlation_id: UUID | str | None
) -> bool:
    """True when this correlation id already has an outcome comment on the PR."""
    if correlation_id is None:
        return False
    owner, repo_name = split_repo(repo)
    needle = f"`{correlation_id}`"
    comments = rest_json(
        "GET",
        f"/repos/{owner}/{repo_name}/issues/{pr_number}/comments?per_page=100",
        token=token,
    )
    if not isinstance(comments, list):
        return False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if isinstance(body, str) and _COMMENT_MARKER in body and needle in body:
            return True
    return False


def report_autobind_outcome(
    *,
    repo: str,
    pr_number: int,
    outcome: EnumAutobindOutcome,
    reason: str,
    correlation_id: UUID | str | None,
    token: str | None,
    head_sha: str | None = None,
) -> bool:
    """Post the durable outcome for one consumed autobind command.

    Returns ``True`` when the check-run was posted, ``False`` otherwise. Never
    raises: a reporting fault must not convert one failure into two.

    Args:
        repo: ``owner/name`` of the PRODUCT repo (never onex_change_control).
        pr_number: The product PR the command named.
        outcome: Terminal disposition.
        reason: Human-readable reason; flattened onto the marker line.
        correlation_id: The published command's correlation id, so the check-run
            on the PR and the record on the bus can be joined.
        token: A GitHub credential with ``checks:write`` on the product repo.
            ``None`` means the run could not resolve one -- logged loudly and
            skipped, because that is itself a reportable condition.
        head_sha: Head SHA to attach the check-run to; re-observed live when
            omitted, never taken from a caller-supplied field that could be
            stale.
    """
    if token is None:
        logger.warning(
            "occ_autobind_outcome: no GitHub credential available to report the "
            "%s outcome on %s#%s (reason=%s, correlation_id=%s). The outcome is "
            "on the bus but NOT on the PR.",
            outcome.value,
            repo,
            pr_number,
            reason,
            correlation_id,
        )
        return False

    try:
        resolved_sha = head_sha or _resolve_head_sha(
            repo=repo, pr_number=pr_number, token=token
        )
    except (GitHubApiError, OSError) as exc:
        logger.warning(
            "occ_autobind_outcome: could not resolve head sha for %s#%s: %s",
            repo,
            pr_number,
            exc,
        )
        return False

    if not resolved_sha:
        logger.warning(
            "occ_autobind_outcome: %s#%s has no resolvable head sha; cannot "
            "attach the %s outcome check-run.",
            repo,
            pr_number,
            outcome.value,
        )
        return False

    owner, repo_name = split_repo(repo)
    summary = render_outcome_summary(
        outcome=outcome,
        reason=reason,
        repo=repo,
        pr_number=pr_number,
        correlation_id=correlation_id,
    )
    posted = False
    try:
        rest_json(
            "POST",
            f"/repos/{owner}/{repo_name}/check-runs",
            token=token,
            body={
                "name": AUTOBIND_OUTCOME_CHECK_NAME,
                "head_sha": resolved_sha,
                "status": "completed",
                "conclusion": _CONCLUSION_BY_OUTCOME[outcome],
                "output": {
                    "title": f"{outcome.value}: {' '.join(str(reason).split())[:120]}",
                    "summary": summary,
                },
            },
        )
        posted = True
    except (GitHubApiError, OSError) as exc:
        logger.warning(
            "occ_autobind_outcome: could not post the %s outcome check-run on "
            "%s#%s: %s",
            outcome.value,
            repo,
            pr_number,
            exc,
        )

    if outcome is not EnumAutobindOutcome.ERROR:
        return posted

    # An ERROR also gets a comment: a red check-run is a line in a rollup, a
    # comment is a notification. Deduplicated per correlation id.
    try:
        if not _comment_already_posted(
            repo=repo,
            pr_number=pr_number,
            token=token,
            correlation_id=correlation_id,
        ):
            rest_json(
                "POST",
                f"/repos/{owner}/{repo_name}/issues/{pr_number}/comments",
                token=token,
                body={
                    "body": _render_comment(
                        outcome=outcome,
                        reason=reason,
                        repo=repo,
                        pr_number=pr_number,
                        correlation_id=correlation_id,
                    )
                },
            )
    except (GitHubApiError, OSError) as exc:
        logger.warning(
            "occ_autobind_outcome: could not post the ERROR outcome comment on "
            "%s#%s: %s",
            repo,
            pr_number,
            exc,
        )

    return posted


__all__ = [
    "AUTOBIND_OUTCOME_CHECK_NAME",
    "OUTCOME_MARKER_PREFIX",
    "EnumAutobindOutcome",
    "render_outcome_summary",
    "report_autobind_outcome",
]
