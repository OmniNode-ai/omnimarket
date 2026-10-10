# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stale CI Summary and pending-required rules of the landing controller (ported from its tests).

FRICTION 2026-10-05T16:27:13Z measured the waste: PRs armed with only ``CI Summary`` red after sibling runs were
cancelled or skipped, which a failed-jobs rerun of the CI run landed within 4 to 25 minutes; a skipped matrix row
whose name was never expanded, which only a fresh head clears; and a worker sent to a PR whose required ``CI Summary``
was still running. A real red beside the summary still goes to a worker (the negative controls).
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_stale_summary import (
    INCIDENT_MIN_RUNS,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_next_stale_step import (
    MAX_STALE_REFRESHES,
)
from tests.unit.nodes.node_pr_lifecycle_triage_compute import (
    landing_rules_adapter as lf,
)

pytestmark = pytest.mark.unit

HEAD = "a" * 40
T = "2026-10-05T15:00:00Z"
MATRIX = (
    "Tests (Split ${{ matrix.split }}/${{ needs.detect-changes.outputs.split_count }})"
)


def _row(
    name: str, conclusion: str, status: str = "completed", at: str = T
) -> list[str]:
    return [name, status, conclusion, at]


def _ci(
    red: list[str],
    rows: list[list[str]],
    head: str = HEAD,
    pending: list[str] | None = None,
) -> dict[str, Any]:
    return {"sha": head, "red": red, "runs": rows, "pending": pending or []}


GREEN = [
    _row("Lint", "success"),
    _row("Tests (Split 1/4)", "success"),
    _row("Docs-Only Marker", "skipped"),
]


# ------------------------------------------------------------------------- class 1: stale summary


def test_a_summary_red_after_a_cancelled_sibling_is_a_rerun() -> None:
    """omnimarket#3424: CI Summary red, dep-health / scan cancelled, everything else green."""
    ci = _ci(
        ["CI Summary"],
        [*GREEN, _row("dep-health / scan", "cancelled"), _row("CI Summary", "failure")],
    )
    assert lf.stale_summary_of(ci, HEAD) == "rerun"


def test_a_cancelled_sibling_named_red_beside_the_summary_is_still_a_rerun() -> None:
    ci = _ci(
        ["CI Summary", "validate"],
        [*GREEN, _row("validate", "cancelled"), _row("CI Summary", "failure")],
    )
    assert lf.stale_summary_of(ci, HEAD) == "rerun"


def test_a_skipped_matrix_row_with_an_unexpanded_name_needs_a_fresh_head() -> None:
    """omnibase_infra#4576: the skipped matrix row clears only on a new head (update-branch)."""
    ci = _ci(
        ["CI Summary"], [*GREEN, _row(MATRIX, "skipped"), _row("CI Summary", "failure")]
    )
    assert lf.stale_summary_of(ci, HEAD) == "refresh"


def _incident_rows(n: int, conclusion: str = "cancelled") -> list[list[str]]:
    return [_row(f"Tests (Split {i}/{n})", conclusion) for i in range(n)]


def test_dozens_of_cancelled_runs_are_a_refresh_first_head() -> None:
    """A provider incident cancelled the head's runs; one rerun can land inside it, a fresh head clears it."""
    n = INCIDENT_MIN_RUNS
    ci = _ci(
        ["CI Summary"], [*GREEN, *_incident_rows(n), _row("CI Summary", "failure")]
    )
    assert lf.stale_summary_of(ci, HEAD) == "refresh_first"
    few = _ci(
        ["CI Summary"], [*GREEN, *_incident_rows(n - 1), _row("CI Summary", "failure")]
    )
    assert (
        lf.stale_summary_of(few, HEAD) == "rerun"
    )  # negative control: a few cancelled keep the one rerun


def test_no_runner_jobs_count_with_the_cancelled_ones_only_in_an_incident() -> None:
    n = INCIDENT_MIN_RUNS
    rows = [
        *GREEN,
        *_incident_rows(n - 2),
        _row("Lint (a)", "startup_failure"),
        _row("Lint (b)", "startup_failure"),
        _row("CI Summary", "failure"),
    ]
    ci = _ci(["CI Summary", "Lint (a)", "Lint (b)"], rows)
    assert lf.stale_summary_of(ci, HEAD) == "refresh_first"
    # negative control: below the incident size a no-runner failure is a real red for a worker
    ci = _ci(
        ["CI Summary", "Lint (a)"],
        [*GREEN, _row("Lint (a)", "startup_failure"), _row("CI Summary", "failure")],
    )
    assert lf.stale_summary_of(ci, HEAD) is None


def test_the_summarys_own_no_runner_copy_counts_toward_the_incident_size() -> None:
    n = INCIDENT_MIN_RUNS
    rows = [
        *GREEN,
        *_incident_rows(n - 1, "startup_failure"),
        _row("CI Summary", "startup_failure"),
    ]
    ci = _ci(
        ["CI Summary", *(r[0] for r in _incident_rows(n - 1, "startup_failure"))], rows
    )
    assert lf.stale_summary_of(ci, HEAD) == "refresh_first"


def test_the_refresh_first_ladder_spends_the_update_branch_before_the_rerun() -> None:
    assert lf.stale_step("refresh_first", None, HEAD) == "update_branch"
    mem = lf.stale_memory_next(None, HEAD, "update_branch", now=T)
    assert (
        lf.stale_step("refresh_first", mem, HEAD) == "rerun"
    )  # same head still stale: the one rerun
    mem = lf.stale_memory_next(mem, HEAD, "rerun", now=T)
    assert lf.stale_step("refresh_first", mem, HEAD) is None
    # the PR's refresh cap is the existing one; spent, the head still has its one rerun
    spent = {"heads": {}, "refreshes": MAX_STALE_REFRESHES, "at": T}
    assert lf.stale_step("refresh_first", spent, "b" * 40) == "rerun"
    assert (
        lf.stale_step(
            "refresh_first",
            lf.stale_memory_next(spent, "b" * 40, "rerun", now=T),
            "b" * 40,
        )
        is None
    )


def test_a_real_red_beside_the_summary_is_not_stale() -> None:
    """Negative control: a failed shard is the PR's own red and goes to a worker."""
    rows = [
        *GREEN,
        _row("Tests (Split 2/4)", "failure"),
        _row("dep-health / scan", "cancelled"),
        _row("CI Summary", "failure"),
    ]
    assert (
        lf.stale_summary_of(_ci(["CI Summary", "Tests (Split 2/4)"], rows), HEAD)
        is None
    )
    # a failed check the watcher did not list as red is still a real red
    assert lf.stale_summary_of(_ci(["CI Summary"], rows), HEAD) is None


def test_a_summary_red_with_nothing_cancelled_or_skipped_is_its_own_red() -> None:
    rows = [_row("Lint", "success"), _row("CI Summary", "failure")]
    assert lf.stale_summary_of(_ci(["CI Summary"], rows), HEAD) is None


def test_a_red_that_is_not_the_summary_or_was_read_at_another_head_is_not_stale() -> (
    None
):
    rows = [*GREEN, _row("Receipt Gate", "cancelled"), _row("CI Summary", "failure")]
    assert (
        lf.stale_summary_of(_ci(["Receipt Gate"], rows), HEAD) is None
    )  # no summary red
    assert lf.stale_summary_of(_ci(["CI Summary"], rows, head="b" * 40), HEAD) is None
    # a red name with no run row (a commit status) is not provably stale
    assert lf.stale_summary_of(_ci(["CI Summary", "deploy-gate"], rows), HEAD) is None


def test_anything_still_running_is_not_stale_yet() -> None:
    rows = [
        *GREEN,
        _row("dep-health / scan", "", status="in_progress"),
        _row("CI Summary", "failure"),
    ]
    assert lf.stale_summary_of(_ci(["CI Summary"], rows), HEAD) is None
    rows = [
        *GREEN,
        _row("dep-health / scan", "cancelled"),
        _row("CI Summary", "failure"),
    ]
    assert (
        lf.stale_summary_of(_ci(["CI Summary"], rows, pending=["CodeQL"]), HEAD) is None
    )


def test_only_the_newest_copy_of_a_check_counts() -> None:
    rows = [
        *GREEN,
        _row("Lint", "failure", at="2026-10-05T14:00:00Z"),
        _row("dep-health / scan", "cancelled"),
        _row("CI Summary", "failure"),
    ]
    assert lf.stale_summary_of(_ci(["CI Summary"], rows), HEAD) == "rerun"


def test_the_stale_ladder_is_one_rerun_then_one_refresh_then_the_decision() -> None:
    assert lf.stale_step("rerun", None, HEAD) == "rerun"
    mem = lf.stale_memory_next(None, HEAD, "rerun", now=T)
    assert lf.stale_step("rerun", mem, HEAD) == "update_branch"
    mem = lf.stale_memory_next(mem, HEAD, "update_branch", now=T)
    assert lf.stale_step("rerun", mem, HEAD) is None  # a worker next
    # the matrix case starts at the refresh
    assert lf.stale_step("refresh", None, HEAD) == "update_branch"
    # a new head starts the per-head ladder again, but refreshes are counted per PR
    mem2 = lf.stale_memory_next(mem, "b" * 40, "rerun", now=T)
    assert lf.stale_step("rerun", mem2, "b" * 40) == "update_branch"
    mem2 = lf.stale_memory_next(mem2, "b" * 40, "update_branch", now=T)
    assert mem2["refreshes"] == MAX_STALE_REFRESHES
    # the PR's refreshes spent: a refresh head gets its one rerun, then the decision (omnibase_infra#4745)
    assert lf.stale_step("refresh", mem2, "c" * 40) == "rerun"
    mem3 = lf.stale_memory_next(mem2, "c" * 40, "rerun", now=T)
    assert lf.stale_step("refresh", mem3, "c" * 40) is None


def test_a_refresh_head_the_update_branch_left_unchanged_gets_its_one_rerun() -> None:
    """omnibase_infra#4745: the update-branch is spent at this head (or the PR's refreshes are), so one rerun."""
    mem = lf.stale_memory_next(None, HEAD, "update_branch", now="2026-10-09T08:00:00Z")
    assert lf.stale_step("refresh", mem, HEAD) == "rerun"
    mem = lf.stale_memory_next(mem, HEAD, "rerun", now="2026-10-09T08:01:00Z")
    assert lf.stale_step("refresh", mem, HEAD) is None
    spent = {"heads": {}, "refreshes": MAX_STALE_REFRESHES}
    assert lf.stale_step("refresh", spent, HEAD) == "rerun"


# ------------------------------------------------------------------------------- class 3: pending


def test_a_pending_required_context_holds_the_worker() -> None:
    ci = _ci(
        [],
        [_row("CI Summary", "", status="in_progress", at="2026-10-05T14:30:00Z")],
        pending=["CI Summary"],
    )
    assert lf.pending_required_of(ci, HEAD, ["CI Summary"], now=T) == ["CI Summary"]


def test_a_pending_check_that_is_not_required_holds_nothing() -> None:
    ci = _ci(
        [],
        [_row("CodeQL", "", status="queued", at="2026-10-05T14:59:00Z")],
        pending=["CodeQL"],
    )
    assert lf.pending_required_of(ci, HEAD, ["CI Summary"], now=T) == []


def test_an_unread_required_set_fails_closed_on_any_pending_check() -> None:
    ci = _ci(
        [],
        [_row("CodeQL", "", status="queued", at="2026-10-05T14:59:00Z")],
        pending=["CodeQL"],
    )
    assert lf.pending_required_of(ci, HEAD, None, now=T) == ["CodeQL"]


def test_a_stuck_pending_context_stops_holding_the_worker() -> None:
    ci = _ci(
        [],
        [_row("CI Summary", "", status="queued", at="2026-10-05T11:00:00Z")],
        pending=["CI Summary"],
    )
    assert lf.pending_required_of(ci, HEAD, ["CI Summary"], now=T) == []
    assert (
        lf.pending_required_of(ci, "b" * 40, ["CI Summary"], now=T) == []
    )  # another head: unknown
