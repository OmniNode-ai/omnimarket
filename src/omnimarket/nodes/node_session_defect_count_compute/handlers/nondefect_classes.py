# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The known non-defect red classes of the ci-watch list, as pure predicates.

A red check-run that falls in one of these classes says nothing about the
pull request's own code, so it is removed before any defect is counted.
"""

from __future__ import annotations

from ..models.model_defect_count import (
    ModelCheckRun,
    ModelHead,
    ModelPullRequest,
    NondefectClass,
)

RED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "startup_failure", "action_required", "stale"}
)

# The change-control cascade set (ci-watch section C).
CASCADE_CHECKS = frozenset(
    {
        "OCC Preflight Dependency",
        "OCC Companion Merged Gate",
        "verify / verify",
    }
)
CASCADE_PREFIX = "occ-preflight"

# Gates that only mirror other checks (ci-watch section F).
FOLLOWER_CHECKS = frozenset(
    {
        "CI Summary",
        "CI Tests Gate",
        "Test-Failure Ratchet Gate",
        "Contract Compliance Check",
    }
)


def is_red(check: ModelCheckRun) -> bool:
    return check.conclusion in RED_CONCLUSIONS


def _order_key(check: ModelCheckRun) -> tuple[int, str]:
    """Newest last; a copy with no completion time sorts newest (ci-watch)."""
    if check.completed_at is None:
        return (1, "")
    return (0, check.completed_at.isoformat())


def _is_cascade(name: str) -> bool:
    return name in CASCADE_CHECKS or name.startswith(CASCADE_PREFIX)


def _predates_ready(check: ModelCheckRun, pr: ModelPullRequest) -> bool:
    return (
        pr.ready_at is not None
        and check.run_created_at is not None
        and check.run_created_at < pr.ready_at
    )


def _superseded(check: ModelCheckRun, head: ModelHead) -> bool:
    """A newer, non-red copy of the same check sits on the same head."""
    mine = _order_key(check)
    return any(
        other.name == check.name
        and other is not check
        and not is_red(other)
        and other.conclusion == "success"
        and _order_key(other) > mine
        for other in head.checks
    )


def classify_red(
    check: ModelCheckRun, head: ModelHead, pr: ModelPullRequest
) -> NondefectClass | None:
    """Return the non-defect class of a red check-run, or None for a real red."""
    if check.failure_kind == "rate_limit":
        return "rate_limit"
    if check.conclusion == "timed_out" or check.failure_kind == "runner_timeout":
        return "runner_timeout"
    if check.conclusion == "cancelled":
        return "cancelled_producer"
    if check.conclusion == "stale" or _superseded(check, head):
        return "stale_copy"
    if _predates_ready(check, pr):
        return "replayed_rerun" if check.run_attempt > 1 else "draft_era_residue"
    if _is_cascade(check.name):
        return "change_control_cascade"
    if check.name in pr.base_red_checks:
        return "base_inherited"
    if check.name in FOLLOWER_CHECKS and any(
        is_red(other) and other.name != check.name for other in head.checks
    ):
        return "follower"
    return None
