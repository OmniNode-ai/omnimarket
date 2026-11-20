# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure decisions of the release cut tick: what a tick does from the train's row and two live reads.

The tick reads the release train's own decision for omnimarket and acts on it. No unreleased
release-relevant commit is nothing to cut. A CUT verdict with no release-cut run in flight is a
dispatch. Every other verdict is a BLOCK that carries the train's own reason, raised once per
(reason, candidate sha) and once more when the oldest unreleased packaged commit passes
``ESCALATE_AGE_HOURS``, well inside the 24 hour staleness window.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_release_cut_tick_effect.models.model_release_cut_tick import (
    ModelReleaseCutOutcome,
)

SCHEMA = "omnimarket-release-cut-tick/v1"
LANE = "omnimarket-release-cut-timer"
ACTOR = "launchd:omnimarket-release-cut"
REPO = "omnimarket"
SLUG = "OmniNode-ai/omnimarket"
WORKFLOW = "release-cut.yml"
#: The staleness gate's window is 24 hours. A block that has held an unreleased commit this long
#: is raised again, so the operator hears about it with a working day left.
ESCALATE_AGE_HOURS = 18.0
PACKAGED_PATHS = ("src", "pyproject.toml")
BOOKKEEPING = (
    re.compile(r"^chore(\([^)]*\))?:\s*post-release dev version bump\b"),
    re.compile(r"^chore(\([^)]*\))?:\s*release\b"),
    re.compile(r"^release:\s"),
)
RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

ACTION_NOTHING_TO_CUT = "nothing-to-cut"
ACTION_CUT_DISPATCHED = "cut-dispatched"
ACTION_BLOCKED = "blocked"
ACTIONS = (ACTION_NOTHING_TO_CUT, ACTION_CUT_DISPATCHED, ACTION_BLOCKED)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def cell(value: str, limit: int = 400) -> str:
    """A value safe inside one ledger cell: no pipe, no line break, bounded."""
    cleaned = " ".join(value.replace("|", "/").split())
    return cleaned[:limit] or "-"


def read_decision(stdout: str) -> dict[str, Any] | None:
    """The omnimarket row of the train's JSON report, or None when the report has none.

    The train prints a markdown table, then the JSON document, on stdout; the JSON starts at the
    first line that is exactly ``{``.
    """
    lines = stdout.splitlines()
    try:
        start = lines.index("{")
    except ValueError:
        return None
    try:
        report = json.loads("\n".join(lines[start:]))
    except json.JSONDecodeError:
        return None
    for row in report.get("decisions", []):
        if isinstance(row, dict) and row.get("repo") == REPO:
            return row
    return None


def decide_outcome(
    *,
    decision: dict[str, Any] | None,
    plan_returncode: int,
    plan_stderr: str,
    age_hours: float | None,
    active_runs: list[dict[str, Any]] | None,
    active_reason: str,
    previous: tuple[str, str],
) -> ModelReleaseCutOutcome:
    """Pure: from the train's row and two live reads to what this tick does."""
    if previous[0] == "failed":
        return ModelReleaseCutOutcome(
            action=ACTION_BLOCKED, reason="release_cut_run_failed", detail=previous[1]
        )
    if decision is None:
        return ModelReleaseCutOutcome(
            action=ACTION_BLOCKED,
            reason="train_decision_unreadable",
            detail=(
                f"release_train.py plan returned rc={plan_returncode} and no {REPO} row: "
                f"{cell(plan_stderr)}"
            ),
        )

    def outcome(action: str, reason: str, detail: str = "") -> ModelReleaseCutOutcome:
        return ModelReleaseCutOutcome.model_validate(
            {
                "action": action,
                "reason": reason,
                "detail": detail,
                "candidate_sha": str(decision.get("candidate_sha", "")),
                "candidate_version": str(decision.get("candidate_version", "")),
                "latest_tag": str(decision.get("latest_tag", "")),
                "unreleased_count": int(decision.get("unreleased_count", 0) or 0),
                "oldest_unreleased_age_hours": age_hours,
            }
        )

    verdict = decision.get("verdict")
    reason = str(decision.get("reason", ""))
    detail = str(decision.get("detail", ""))
    if verdict == "SKIP" and reason == "no_unreleased_release_relevant_work":
        return outcome(ACTION_NOTHING_TO_CUT, reason, detail)
    if verdict != "CUT":
        return outcome(ACTION_BLOCKED, reason or "train_verdict_unnamed", detail)
    if decision.get("needs_bump"):
        return outcome(
            ACTION_BLOCKED,
            "dev_version_not_ahead_of_tag",
            f"dev's version {decision.get('dev_version')} is not ahead of "
            f"{decision.get('latest_tag')}"
            "; release-cut.yml refuses that and the post-release bump PR has not landed",
        )
    if active_runs is None:
        return outcome(ACTION_BLOCKED, "release_cut_runs_unreadable", active_reason)
    if active_runs:
        # A cut is already under way: waiting for it is the correct action, not a block.
        return outcome(
            ACTION_NOTHING_TO_CUT,
            "release_cut_run_in_flight",
            f"{len(active_runs)} release-cut run(s) queued or in progress: "
            + ", ".join(
                str(row.get("url", row.get("databaseId"))) for row in active_runs
            ),
        )
    return outcome(ACTION_CUT_DISPATCHED, reason, detail)


def block_key(outcome: ModelReleaseCutOutcome) -> str:
    return f"{outcome.reason}@{outcome.candidate_sha}"


def should_raise(outcome: ModelReleaseCutOutcome, state: dict[str, Any]) -> bool:
    """Edge-triggered: a new (reason, sha), or the first tick past the escalation age."""
    if outcome.action != ACTION_BLOCKED:
        return False
    if state.get("last_block_key") != block_key(outcome):
        return True
    age = outcome.oldest_unreleased_age_hours
    return bool(
        age is not None
        and age >= ESCALATE_AGE_HOURS
        and not state.get("escalated_at_age")
    )


def status_row(
    outcome: ModelReleaseCutOutcome,
    now: datetime,
    *,
    escalated: bool,
    ticket: str = "",
) -> str:
    cells = [
        stamp(now),
        "STATUS",
        f"lane={LANE}",
        f"actor={ACTOR}",
        "model=none",
    ]
    if ticket:
        cells.append(f"ticket={cell(ticket, 40)}")
    cells += [
        f"outcome={outcome.action}",
        f"reason={cell(outcome.reason, 80)}",
        f"repo={REPO}",
        f"candidate={outcome.candidate_sha[:12] or '-'}",
        f"version={outcome.candidate_version or '-'}",
        f"latest_tag={outcome.latest_tag or '-'}",
        f"unreleased={outcome.unreleased_count}",
    ]
    if outcome.oldest_unreleased_age_hours is not None:
        cells.append(
            f"oldest_unreleased_age_h={outcome.oldest_unreleased_age_hours:.1f}"
            f" (staleness gate at 24)"
        )
    if escalated:
        cells.append(f"flag=escalated-past-{ESCALATE_AGE_HOURS:.0f}h")
    if outcome.run_url:
        cells.append(f"run={outcome.run_url}")
    cells.append(f"detail={cell(outcome.detail, 700)}")
    return " | ".join(cells)
