# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readiness plan's tickets, grouped by where each stands (OMN-23081)."""

from __future__ import annotations

from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_tracker import (
    build_tracker,
    evaluate_start,
    plan_ticket_ids,
    referenced_tickets,
    start_text,
)

from .support import NOW, RT, inputs, lane, mirror, pr


def _tickets(**kwargs):  # type: ignore[no-untyped-def]
    doc = build_tracker(RT, inputs(**kwargs), NOW)
    return {t.id: t for t in doc.tickets}, doc


def test_the_plan_is_the_twenty_three_tickets_of_the_range() -> None:
    ids = plan_ticket_ids(RT)
    assert ids[0] == "OMN-23161"
    assert ids[-1] == "OMN-23183"
    assert len(ids) == 23
    _, doc = _tickets()
    assert [t.id for t in doc.tickets] == list(ids)
    assert doc.counts.tickets_total == 23


def test_each_group_is_reached_from_its_own_source() -> None:
    tickets, doc = _tickets(
        mirrors=(mirror(23161, status_type="completed"),),
        prs=(
            pr(23162, "merged", number=1),
            pr(23163, "open", "red", number=2),
            pr(23164, "open", "green", number=3),
        ),
        lanes=(lane(23165, "h053"),),
    )
    assert tickets["OMN-23161"].group == "done"
    assert tickets["OMN-23162"].group == "merged_awaiting_closer"
    assert tickets["OMN-23163"].group == "pr_open"
    assert tickets["OMN-23163"].pr_ci == "red"
    assert tickets["OMN-23164"].group == "pr_open"
    assert tickets["OMN-23164"].pr_ci == "green"
    assert tickets["OMN-23165"].group == "lane_running"
    assert tickets["OMN-23165"].lane == "lane-23165"
    assert tickets["OMN-23165"].host == "h053"
    assert tickets["OMN-23166"].group == "not_started"
    by_group = {g.id: g.count for g in doc.counts.groups}
    assert by_group == {
        "done": 1,
        "merged_awaiting_closer": 1,
        "pr_open": 2,
        "lane_running": 1,
        "not_started": 18,
        "unread": 0,
    }
    assert sum(by_group.values()) == 23
    assert (doc.counts.pr_open_red, doc.counts.pr_open_green) == (1, 1)


def test_done_wins_over_an_open_pull_request() -> None:
    tickets, _ = _tickets(
        mirrors=(mirror(23161, status_type="completed"),), prs=(pr(23161, "open"),)
    )
    assert tickets["OMN-23161"].group == "done"


def test_one_open_pull_request_keeps_a_ticket_open_though_another_merged() -> None:
    tickets, _ = _tickets(
        prs=(
            pr(23169, "merged", number=1),
            pr(23169, "open", "red", "omnibase_infra", 2),
        )
    )
    t = tickets["OMN-23169"]
    assert t.group == "pr_open"
    assert {p.ref for p in t.prs} == {"omnimarket#1", "omnibase_infra#2"}
    assert t.pr_ci == "red"


def test_a_red_pull_request_makes_the_ticket_red_whatever_the_others_are() -> None:
    tickets, _ = _tickets(
        prs=(pr(23161, "open", "green", number=1), pr(23161, "open", "red", number=2))
    )
    assert tickets["OMN-23161"].pr_ci == "red"


def test_a_closed_unmerged_pull_request_is_not_progress() -> None:
    tickets, _ = _tickets(prs=(pr(23161, "closed"),))
    t = tickets["OMN-23161"]
    assert t.group == "not_started"
    assert "closed without merging" in t.why


def test_a_dead_or_stalled_lane_is_not_a_lane_running() -> None:
    tickets, _ = _tickets(
        lanes=(lane(23161, liveness="dead"), lane(23162, liveness="finished"))
    )
    assert tickets["OMN-23161"].group == "not_started"
    assert tickets["OMN-23162"].group == "not_started"


def test_an_unreadable_watcher_never_reads_as_not_started() -> None:
    tickets, doc = _tickets(
        watcher=False, mirrors=(mirror(23161, status_type="completed"),)
    )
    assert tickets["OMN-23161"].group == "done"
    assert tickets["OMN-23162"].group == "unread"
    assert doc.counts.groups[-1].count == 22


def test_an_unreadable_floor_record_never_hides_a_lane() -> None:
    tickets, _ = _tickets(record=False)
    assert tickets["OMN-23162"].group == "unread"


# ---- start conditions ----------------------------------------------------------------------------------------------------


def test_start_text_and_references_drop_the_markdown_links() -> None:
    raw = "A3c ([OMN-23162](https://tracker.example.invalid/issue/OMN-23162)) and K7a ([OMN-23163](https://tracker.example.invalid/issue/OMN-23163)) have both merged"
    assert start_text(raw) == "A3c (OMN-23162) and K7a (OMN-23163) have both merged"
    assert referenced_tickets(raw) == ("OMN-23162", "OMN-23163")


def test_a_start_condition_holds_when_each_ticket_it_names_has_merged() -> None:
    start = "A3c ([OMN-23162](x)) and K7a ([OMN-23163](x)) have both merged and been released"
    tickets, _ = _tickets(
        mirrors=(mirror(23164, start_when=start),),
        prs=(pr(23162, "merged", number=1), pr(23163, "merged", number=2)),
    )
    t = tickets["OMN-23164"]
    assert t.group == "not_started"
    assert t.start_condition is True
    assert all(p.met for p in t.start_parts)
    assert "not read" in t.start_note


def test_control_a_start_condition_is_false_while_one_named_ticket_is_still_open() -> (
    None
):
    start = "A3c ([OMN-23162](x)) and K7a ([OMN-23163](x)) have both merged"
    tickets, _ = _tickets(
        mirrors=(mirror(23164, start_when=start),),
        prs=(pr(23162, "merged", number=1), pr(23163, "open", "green", number=2)),
    )
    t = tickets["OMN-23164"]
    assert t.start_condition is False
    assert [p.met for p in t.start_parts] == [True, False]


def test_an_operator_question_counts_only_when_the_contract_records_the_answer() -> (
    None
):
    start = "the operator answers Q2 (paid models), and A1 ([OMN-23171](x)) has merged"
    answered, _ = _tickets(
        mirrors=(mirror(23181, start_when=start),), prs=(pr(23171, "merged"),)
    )
    assert answered["OMN-23181"].start_condition is True
    unanswered = RT.model_copy(update={"operator_answers": ()})
    doc = build_tracker(
        unanswered,
        inputs(mirrors=(mirror(23181, start_when=start),), prs=(pr(23171, "merged"),)),
        NOW,
    )
    assert next(t for t in doc.tickets if t.id == "OMN-23181").start_condition is False


def test_a_ticket_outside_the_plan_that_nothing_reads_is_unread_not_false() -> None:
    met, words, parts, _ = evaluate_start(
        "the market ingress accepts a model ([OMN-20844](x) step 2)", {}, {}, {}, set()
    )
    assert met is None
    assert parts[0].met is None
    assert "OMN-20844" in words


def test_no_start_condition_written_means_it_can_start() -> None:
    tickets, doc = _tickets(mirrors=(mirror(23161),))
    assert tickets["OMN-23161"].start_condition is True
    assert doc.counts.start_true >= 1


def test_a_ticket_missing_from_the_mirror_is_unread_for_its_start_condition() -> None:
    tickets, _ = _tickets()
    t = tickets["OMN-23161"]
    assert t.group == "not_started"
    assert t.start_condition is None
    assert t.linear_status == "unread"
