# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reviewer capacity regressions from the 2026-10-26T00:47Z controller sweep."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_count_reviewer_runs_in_flight import (
    HandlerCountReviewerRunsInFlight,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_runs_facts import (
    ModelReviewerRunsFacts,
)

pytestmark = pytest.mark.unit
NOW = "2026-10-26T00:47:00Z"
HEAD = "02e852a49a9fd6a9a96c1df4fe88fcdb32293f75"
VOTER = "Hostile Reviewer (adversarial gate)"


def _record(
    *, draft: bool = False, read_at: str = NOW, name: str = VOTER
) -> dict[str, Any]:
    return {
        "facts": {"state": "OPEN", "draft": draft, "head_sha": HEAD},
        "ci": {
            "sha": HEAD,
            "read_at": read_at,
            "runs": [[name, "in_progress", "", ""]],
        },
    }


def _count(records: dict[str, Any]) -> int:
    return (
        HandlerCountReviewerRunsInFlight()
        .handle(ModelReviewerRunsFacts.model_validate({"records": records, "now": NOW}))
        .in_flight
    )


def test_draft_reviewer_does_not_hold_a_slot() -> None:
    assert _count({"omnimarket#4409": _record(draft=True)}) == 0


def test_stale_watcher_ci_read_does_not_hold_a_slot() -> None:
    assert _count({"omnimarket#4409": _record(read_at="2026-10-25T15:15:35Z")}) == 0


def test_hostile_review_gate_summary_does_not_hold_a_voter_slot() -> None:
    assert _count({"omniclaude#2981": _record(name="Hostile Review Gate")}) == 0


@pytest.mark.parametrize(
    "status", ["queued", "in_progress", "waiting", "pending", "requested"]
)
@pytest.mark.parametrize("read_at", [NOW, "2026-10-26T00:39:00Z"])
def test_fresh_voters_count_through_the_eight_minute_boundary(
    status: str, read_at: str
) -> None:
    rec = _record(read_at=read_at)
    rec["ci"]["runs"][0][1] = status
    assert _count({"omnimarket#1": rec}) == 1


@pytest.mark.parametrize(
    "read_at", ["2026-10-26T00:38:59Z", "", "invalid", "2026-10-26T00:47:01Z"]
)
def test_unusable_watcher_reads_do_not_hold_slots(read_at: str) -> None:
    assert _count({"omnimarket#1": _record(read_at=read_at)}) == 0


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_closed_prs_do_not_hold_slots(state: str) -> None:
    rec = _record()
    rec["facts"]["state"] = state
    assert _count({"omnimarket#1": rec}) == 0


@pytest.mark.parametrize("sha", [None, "another-head"])
def test_ci_from_another_or_unknown_head_does_not_hold_a_slot(sha: str | None) -> None:
    rec = _record()
    rec["ci"]["sha"] = sha
    assert _count({"omnimarket#1": rec}) == 0


def test_only_active_voter_rows_consume_capacity() -> None:
    rec = _record()
    rec["ci"]["runs"] += [
        ["Hostile Review Gate", "queued", "", ""],
        ["Tests", "in_progress", "", ""],
        [VOTER, "completed", "success", NOW],
        [VOTER],
    ]
    assert _count({"omnimarket#1": rec}) == 1


def test_recorded_false_occupancy_frees_the_existing_two_slot_pool() -> None:
    from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_apply_reviewer_pool import (
        apply_reviewer_pool,
    )

    records = {
        "omnimarket#4409": _record(draft=True, read_at="2026-10-25T15:15:35Z"),
        "omnimarket#2": _record(read_at="2026-10-25T15:15:35Z"),
        "omnimarket#3": _record(name="Hostile Review Gate"),
    }
    target = {
        "pr": "omniclaude#2981",
        "head_sha": "c5828a77",
        "created_at": "2026-10-25T15:00:00Z",
        "ci": "red",
        "red_class": "reviewer_pool",
        "red_checks": [VOTER, "Hostile Review Gate"],
    }
    out = apply_reviewer_pool(
        [target], state_records=[], in_flight=_count(records), slots=2
    )
    assert out[0]["red_class"] == "runner_saturation"
    records.update({"omnimarket#4": _record(), "omnimarket#5": _record()})
    full = apply_reviewer_pool(
        [target], state_records=[], in_flight=_count(records), slots=2
    )
    assert full[0]["ci"] == "pending"
    assert full[0]["red_checks"] == []
