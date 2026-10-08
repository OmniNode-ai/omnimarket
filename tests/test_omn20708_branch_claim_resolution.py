# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical reference cases against database-row replay."""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_branch_claim_check_effect.handlers.branch_claim_resolution import (
    build_index,
    commit_identity,
    resolve_with_index,
)
from omnimarket.nodes.node_branch_claim_check_effect.models import (
    load_branch_claim_policy,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_branch_claim_check_effect/contract.yaml"
)
POLICY = load_branch_claim_policy(CONTRACT)


def row(kind="CLAIM", lane="lane-a", ticket="OMN-1", hours=2, extra=""):
    stamp = (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} | {kind} | lane={lane} | tickets={ticket} | {extra or 'taking the scope'}"


def db_rows(raws):
    return tuple(
        (
            hashlib.sha256(raw.encode()).hexdigest(),
            datetime.fromisoformat(raw.split(" | ")[0]),
            raw,
        )
        for raw in raws
    )


def commit(lane="lane-a", fence=None):
    message = f"fix: change\n\nOnex-Lane: {lane}\nOnex-Session: session\n"
    if fence is not None:
        message += f"Onex-Fence: {fence}\n"
    return "a" * 40, message


def resolve(raws, commits=None, branch="lane/omn-1-change", policy=POLICY):
    index = build_index(db_rows(raws), now=NOW, staleness_hours=policy.staleness_hours)
    return resolve_with_index(
        index,
        branch=branch,
        commits=[commit()] if commits is None else commits,
        policy=policy,
    )


@pytest.mark.parametrize(
    ("raws", "commits", "branch", "outcome", "fence"),
    [
        ([row()], [commit()], "omn-1", "held-by-pusher", 1),
        (
            [row(), row("RELEASE", hours=1)],
            [commit("lane-b")],
            "omn-1",
            "unclaimed",
            None,
        ),
        (
            [row(), row("HANDOVER", hours=1, extra="to=lane-b")],
            [commit("lane-b")],
            "omn-1",
            "held-by-pusher",
            2,
        ),
        ([row()], [commit("lane-b")], "omn-1", "held-elsewhere", 1),
        ([], [commit()], "omn-1", "unclaimed", None),
        (
            [row(), row("TERMINAL", hours=1)],
            [commit("lane-b")],
            "omn-1",
            "unclaimed",
            None,
        ),
        ([row()], [("a" * 40, "no trailers")], "omn-1", "unidentified", 1),
        ([row()], [commit()], "main", "no-ticket", None),
        ([row(hours=13)], [commit("lane-b")], "omn-1", "unclaimed", None),
        ([row()], [commit(), commit("lane-b")], "omn-1", "held-elsewhere", 1),
        ([row()], [commit(fence=0)], "omn-1", "fence-behind", 1),
        (
            [row(), row("TERMINAL", "lane-b", hours=1)],
            [commit("lane-b")],
            "omn-1",
            "held-elsewhere",
            1,
        ),
        (
            [row(), row("TERMINAL", ticket="OMN-1,OMN-2", hours=1)],
            [commit()],
            "omn-1",
            "unclaimed",
            None,
        ),
        (
            [row(), row("TERMINAL", hours=1), row(lane="lane-b", hours=0)],
            [commit("lane-b")],
            "omn-1",
            "held-by-pusher",
            2,
        ),
        ([row()], [], "omn-1", "held-by-pusher", 1),
        ([], [], "omn-1", "unclaimed", None),
        ([row()], [commit(fence="oops")], "omn-1", "held-by-pusher", 1),
    ],
)
def test_resolution_cases(raws, commits, branch, outcome, fence):
    verdict = resolve(raws, commits, branch)
    assert verdict.outcome.value == outcome
    assert (verdict.holder.fence if verdict.holder else None) == fence
    if outcome in {"held-elsewhere", "fence-behind"}:
        assert "work_ledger_rows:" in verdict.findings[0]
        assert "RELEASE" in verdict.findings[0]
        assert "CLAIM" in verdict.findings[0]
    if outcome in {"held-by-pusher", "unclaimed", "no-ticket"}:
        assert not verdict.findings


def test_activity_and_same_lane_claim_renew_without_advancing_fence():
    verdict = resolve([row(hours=15), row("STATUS", hours=5), row(hours=1)])
    assert verdict.holder.fence == 1
    assert verdict.holder.claimed_at == row(hours=15).split(" | ")[0]
    assert verdict.holder.last_activity_at == row(hours=1).split(" | ")[0]


@pytest.mark.parametrize(
    ("hours", "extra", "expected"),
    [
        (1, "stale=file:1", "lane-a"),
        (13, "no citation", None),
        (13, "stale=file:1", "lane-b"),
    ],
)
def test_reclaim_requires_staleness_and_citation(hours, extra, expected):
    verdict = resolve(
        [row(hours=hours), row("RECLAIM", "lane-b", hours=0, extra=extra)],
        [commit("lane-b")],
    )
    assert (verdict.holder.lane if verdict.holder else None) == expected


def test_claim_collision_and_exact_declared_subjects():
    verdict = resolve(
        [
            row(),
            row(lane="lane-b", hours=1),
            row("TERMINAL", ticket="OMN-2", hours=0, extra="related OMN-1"),
        ],
        [commit("lane-b")],
    )
    assert verdict.holder.lane == "lane-a"
    assert verdict.lanes == ("lane-b",)


@pytest.mark.parametrize(
    "message",
    [
        "Onex-Lane: lane-a\nOnex-Session: s\n\nprose after trailers",
        "x\n\nOnex-Lane: Bad_Lane\nOnex-Session: s",
        "x\n\nOnex-Lane: lane-a",
        "x\n\nOnex-Lane: " + "a" * 65 + "\nOnex-Session: s",
    ],
)
def test_unresolvable_identity(message):
    assert commit_identity(message, POLICY.max_lane_length) is None


def test_comments_stripped_and_final_duplicate_wins():
    message = "fix\n\nOnex-Lane: old\nOnex-Lane: lane-a\nOnex-Session: s\n\n# comment\n# trailer-looking comment"
    assert commit_identity(message, POLICY.max_lane_length) == ("lane-a", "s")


def test_staleness_boundary_and_sorted_database_order():
    assert resolve([row(hours=12)]).outcome.value == "held-by-pusher"
    assert (
        resolve([row("TERMINAL", hours=1), row(hours=2)]).outcome.value == "unclaimed"
    )


def test_a_claim_and_its_terminal_in_one_second_leave_no_holder():
    claim, terminal = row(hours=1), row("TERMINAL", hours=1)
    stamp = datetime.fromisoformat(claim.split(" | ")[0])
    # row_id order puts the TERMINAL first: the replay must not follow it.
    planted = (("f" * 64, stamp, claim), ("0" * 64, stamp, terminal))
    index = build_index(planted, now=NOW, staleness_hours=POLICY.staleness_hours)
    assert index == {}


def test_terminal_releases_every_declared_ticket_and_remembers_each_fence():
    rows = [
        row(ticket="OMN-1,OMN-2"),
        row("TERMINAL", ticket="OMN-1,OMN-2", hours=1),
        row(lane="lane-b", ticket="OMN-1,OMN-2", hours=0),
    ]
    index = build_index(db_rows(rows), now=NOW, staleness_hours=POLICY.staleness_hours)
    assert {ticket: held.fence for ticket, held in index.items()} == {
        "OMN-1": 2,
        "OMN-2": 2,
    }


def test_outcome_severity_keeps_all_findings_and_lanes():
    verdict = resolve(
        [row()], [commit("lane-b"), ("b" * 40, "anonymous"), commit(fence=0)]
    )
    assert verdict.outcome.value == "held-elsewhere"
    assert len(verdict.findings) == 3
    assert verdict.lanes == ("lane-b", "lane-a")


def test_continuation_text_cannot_change_declared_subjects_or_lane():
    raw = row(ticket="OMN-2") + "\n | ticket=OMN-1 | lane=lane-b"
    assert resolve([raw]).outcome.value == "unclaimed"
