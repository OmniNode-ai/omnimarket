# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""beta_board: the state rules (OMN-23315 AC1). Each criterion's state comes from its readings; a reading that is missing is never a pass."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_beta_board_effect.handlers.compute_beta_board import (
    build_board,
    source_state,
)
from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelBetaBoardOverlay,
)
from tests.nodes.node_beta_board_effect.support import (
    NOW,
    OVERLAY,
    ago,
    overlay,
    read,
)


def all_pass_reads():
    return {
        "probe-local": read(
            "probe-local",
            "workflow_run",
            (ago(minutes=30), "pass"),
            (ago(hours=3), "pass"),
        ),
        "probe-cloud": read("probe-cloud", "workflow_run", (ago(minutes=20), "pass")),
        "ledger-one": read("ledger-one", "ledger_row", (ago(minutes=5), "pass")),
    }


def by_id(board):
    return {c.id: c for c in board.criteria}


def test_beta_board_every_reading_passing_still_leaves_a_criterion_with_no_reading_unmet() -> (
    None
):
    board = build_board(overlay(), all_pass_reads(), NOW)
    states = {c.id: c.state for c in board.criteria}
    assert states == {"T1": "pass", "T2": "pass", "T3": "no_reading"}
    assert board.met is False
    assert board.counts["no_reading"] == 1


def test_beta_board_met_only_when_every_criterion_has_a_passing_reading() -> None:
    raw = {**OVERLAY, "criteria": OVERLAY["criteria"][:2]}
    board = build_board(
        ModelBetaBoardOverlay.model_validate(raw), all_pass_reads(), NOW
    )
    assert board.met is True


def test_beta_board_removing_a_criterions_reading_turns_it_unread_and_unmeets_the_board() -> (
    None
):
    raw = {**OVERLAY, "criteria": OVERLAY["criteria"][:2]}
    ov = ModelBetaBoardOverlay.model_validate(raw)
    reads = all_pass_reads()
    del reads["probe-cloud"]  # the injected break: this criterion's reading is gone
    board = build_board(ov, reads, NOW)
    t2 = by_id(board)["T2"]
    assert t2.state == "unread"
    assert "probe-cloud" in t2.reason
    assert t2.since is None
    assert board.met is False


def test_beta_board_one_failing_source_fails_the_criterion_and_since_is_the_start_of_the_failing_run() -> (
    None
):
    reads = all_pass_reads()
    reads["probe-cloud"] = read(
        "probe-cloud",
        "workflow_run",
        (ago(minutes=20), "fail"),
        (ago(hours=1), "fail"),
        (ago(hours=2), "pass"),
    )
    t2 = by_id(build_board(overlay(), reads, NOW))["T2"]
    assert t2.state == "fail"
    assert t2.since == ago(hours=1)
    assert t2.since_is_lower_bound is False
    assert t2.reading_age_seconds == 1200


def test_beta_board_since_is_only_a_lower_bound_when_the_streak_reaches_the_oldest_reading() -> (
    None
):
    t1 = by_id(build_board(overlay(), all_pass_reads(), NOW))["T1"]
    assert t1.state == "pass"
    assert t1.since == ago(hours=3)
    assert t1.since_is_lower_bound is True


def test_beta_board_a_pass_criterion_passes_since_its_last_source_began_to_pass() -> (
    None
):
    reads = all_pass_reads()
    reads["probe-cloud"] = read(
        "probe-cloud",
        "workflow_run",
        (ago(minutes=20), "pass"),
        (ago(minutes=50), "pass"),
        (ago(hours=2), "fail"),
    )
    reads["ledger-one"] = read(
        "ledger-one", "ledger_row", (ago(minutes=5), "pass"), (ago(minutes=10), "fail")
    )
    t2 = by_id(build_board(overlay(), reads, NOW))["T2"]
    assert t2.state == "pass"
    assert t2.since == ago(minutes=5)


def test_beta_board_a_reading_older_than_its_bound_is_stale_not_pass() -> None:
    reads = all_pass_reads()
    reads["probe-local"] = read("probe-local", "workflow_run", (ago(hours=3), "pass"))
    t1 = by_id(build_board(overlay(), reads, NOW))["T1"]
    assert t1.state == "stale"
    assert t1.since == ago(hours=3)


def test_beta_board_fail_outranks_unread_and_stale() -> None:
    reads = {
        "probe-local": read("probe-local", "workflow_run", (ago(hours=9), "pass")),
        "probe-cloud": read("probe-cloud", "workflow_run", (ago(minutes=1), "fail")),
    }
    t2 = by_id(build_board(overlay(), reads, NOW))["T2"]
    assert t2.state == "fail"  # ledger-one is unread and probe-cloud fails


def test_beta_board_an_unknown_newest_verdict_and_an_unreadable_source_are_unread() -> (
    None
):
    unknown = source_state(
        read("x", "ledger_row", (ago(minutes=1), "unknown")),
        source_id="x",
        kind="ledger_row",
        max_age_seconds=7200,
        now=NOW,
    )
    assert unknown.state == "unread"
    missing = source_state(
        None, source_id="x", kind="ledger_row", max_age_seconds=7200, now=NOW
    )
    assert missing.state == "unread"
    empty = source_state(
        read("x", "ledger_row"),
        source_id="x",
        kind="ledger_row",
        max_age_seconds=7200,
        now=NOW,
    )
    assert empty.state == "unread"


def test_beta_board_owner_ticket_and_carriers_come_from_the_overlay() -> None:
    t1 = by_id(build_board(overlay(), all_pass_reads(), NOW))["T1"]
    assert t1.owner_ticket == "OMN-1"
    assert t1.carriers == ("OMN-1", "OMN-2")


def test_beta_board_lists_the_probes_still_read_from_a_cloud_repository() -> None:
    board = build_board(overlay(), all_pass_reads(), NOW)
    assert [(p.probe_id, p.repository, p.criteria) for p in board.cloud_probes] == [
        ("probe-cloud", "Org/cloudrepo", ("T2",))
    ]


def test_beta_board_no_cloud_repository_declared_lists_no_probe() -> None:
    ov = ModelBetaBoardOverlay.model_validate({**OVERLAY, "cloud_repositories": []})
    assert build_board(ov, all_pass_reads(), NOW).cloud_probes == ()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d["criteria"][0].update(probes=["ghost"]), "ghost"),
        (lambda d: d["criteria"][1].update(ledger_readings=["ghost"]), "ghost"),
        (lambda d: d["criteria"].append(dict(d["criteria"][0])), "duplicate criterion"),
    ],
)
def test_beta_board_overlay_refuses_a_criterion_naming_a_reading_it_does_not_declare(
    mutation, message
) -> None:
    import copy

    raw = copy.deepcopy(OVERLAY)
    mutation(raw)
    with pytest.raises(ValidationError, match=message):
        ModelBetaBoardOverlay.model_validate(raw)
