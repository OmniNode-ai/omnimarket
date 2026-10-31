# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_ask_state_compute: the state of each operator ask on fixture prompts and ledger rows (OMN-21713)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_ask_state_compute as compute_node
from omnimarket.nodes.node_ask_state_compute.handlers.handler_ask_state import (
    HandlerAskState,
)
from omnimarket.nodes.node_ask_state_compute.models.model_ask_state import (
    EnumAskLink,
    EnumAskState,
    ModelAskEntry,
    ModelAskPrompt,
    ModelAskPromptItem,
    ModelAskStateConfig,
    ModelAskStateReport,
    ModelAskStateRequest,
)

pytestmark = pytest.mark.unit

SINCE = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
SAID = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
CONFIG = ModelAskStateConfig.model_validate(
    yaml.safe_load(
        Path(compute_node.__file__)
        .with_name("contract.yaml")
        .read_text(encoding="utf-8")
    )["config"]
)


def _prompt(
    n: int, quote: str, kind: str = "ask", subject: str = "", said: datetime = SAID
) -> ModelAskPrompt:
    return ModelAskPrompt(
        capture_id=f"cap{n:04d}",
        session_id="sess-1",
        said_at=said,
        text=quote,
        items=(
            ModelAskPromptItem(
                item_id=f"cap-{n:010x}",
                kind=kind,
                quote=quote,
                subject=subject,
                confidence=0.5,
            ),
        ),
    )


def ask_id(n: int) -> str:
    return f"ask-{n:010x}"


def _run(
    prompts: tuple[ModelAskPrompt, ...],
    rows: tuple[str, ...] = (),
    now: datetime = LATER,
    config: ModelAskStateConfig = CONFIG,
) -> ModelAskStateReport:
    return HandlerAskState().handle(
        ModelAskStateRequest(
            prompts=prompts, rows=rows, now=now, since=SINCE, config=config
        )
    )


def _state(report: ModelAskStateReport, n: int) -> EnumAskState:
    return next(e.state for e in report.asks if e.ask_id == ask_id(n))


def _entry(report: ModelAskStateReport, n: int) -> ModelAskEntry:
    return next(e for e in report.asks if e.ask_id == ask_id(n))


def claim(lane: str, stamp: str, ask: str, ticket: str = "OMN-1") -> str:
    return (
        f"{stamp} | CLAIM | lane={lane} | ticket={ticket} | ask={ask} | worktree=none | "
        "est ~1 lane-hours; displaces nothing; (OMN-1)"
    )


def terminal(
    lane: str, stamp: str, claim_stamp: str, outcome: str, extra: str = ""
) -> str:
    return (
        f"{stamp} | TERMINAL | lane={lane} | friction=none | closes-CLAIM={claim_stamp} | "
        f"outcome={outcome}{extra} | delegated=0"
    )


def test_ask_answered_in_chat_only_is_dropped() -> None:
    report = _run((_prompt(1, "Check the MSK topic list and send it"),))
    assert _state(report, 1) is EnumAskState.DROPPED
    assert report.dropped == (ask_id(1),)
    assert report.counts.dropped == 1
    assert _entry(report, 1).link is EnumAskLink.NONE
    assert "DROPPED (1)" in report.text


def test_young_ask_with_no_row_is_new_not_dropped() -> None:
    report = _run(
        (_prompt(1, "Draft the retro"),), now=datetime(2026, 10, 31, 8, 10, tzinfo=UTC)
    )
    assert _state(report, 1) is EnumAskState.NEW
    assert report.dropped == ()


def test_ask_that_spawned_a_lane_is_owned_then_done() -> None:
    claim_row = claim("lane-a", "2026-10-31T08:05:00Z", ask_id(2))
    owned = _run((_prompt(2, "Mint the child ticket"),), (claim_row,))
    entry = _entry(owned, 2)
    assert entry.state is EnumAskState.OWNED
    assert entry.owner_lane == "lane-a"
    assert entry.link is EnumAskLink.EXPLICIT
    assert owned.wip.owned_lanes == ("lane-a",)

    done_row = terminal(
        "lane-a", "2026-10-31T09:00:00Z", "2026-10-31T08:05:00Z", "done"
    )
    done = _run((_prompt(2, "Mint the child ticket"),), (claim_row, done_row))
    assert _state(done, 2) is EnumAskState.DONE
    assert _entry(done, 2).resolution == "terminal"


def test_failed_lane_with_a_ticket_leaves_the_ask_ticketed_without_one_it_is_dropped() -> (
    None
):
    claim_row = claim("lane-a", "2026-10-31T08:05:00Z", ask_id(3), ticket="OMN-77")
    blocked = terminal(
        "lane-a", "2026-10-31T09:00:00Z", "2026-10-31T08:05:00Z", "blocked"
    )
    report = _run((_prompt(3, "Fix the swap"),), (claim_row, blocked))
    entry = _entry(report, 3)
    assert entry.state is EnumAskState.TICKETED
    assert entry.tickets == ("OMN-77",)

    bare_claim = claim_row.replace("ticket=OMN-77", "ticket=none")
    report = _run((_prompt(3, "Fix the swap"),), (bare_claim, blocked))
    assert _state(report, 3) is EnumAskState.DROPPED


def test_handed_off_lane_still_owns_the_ask() -> None:
    claim_row = claim("lane-a", "2026-10-31T08:05:00Z", ask_id(4))
    handed = terminal(
        "lane-a",
        "2026-10-31T09:00:00Z",
        "2026-10-31T08:05:00Z",
        "handed-off",
        " | handed_to=landing-controller",
    )
    report = _run((_prompt(4, "Ship the node"),), (claim_row, handed))
    entry = _entry(report, 4)
    assert entry.state is EnumAskState.OWNED
    assert entry.owner_lane == "landing-controller"


def test_parked_asks_queue_by_sequence_then_age_and_keep_their_reason() -> None:
    prompts = (
        _prompt(5, "Rack the new host", said=datetime(2026, 10, 31, 1, tzinfo=UTC)),
        _prompt(6, "Add little animations", said=datetime(2026, 10, 31, 2, tzinfo=UTC)),
        _prompt(7, "Try the NVMe card", said=datetime(2026, 10, 31, 3, tzinfo=UTC)),
    )
    rows = (
        f"2026-10-31T04:00:00Z | STATUS | lane=orch | ask={ask_id(5)} | state=parked | reason=waiting on hardware | seq=3",
        f"2026-10-31T04:01:00Z | STATUS | lane=orch | ask={ask_id(6)} | state=parked | reason=after the floor ships | seq=1",
        f"2026-10-31T04:02:00Z | STATUS | lane=orch | ask={ask_id(7)} | state=parked | reason=no slot",
    )
    report = _run(prompts, rows)
    assert [p.ask_id for p in report.parked_queue] == [ask_id(6), ask_id(5), ask_id(7)]
    assert [p.position for p in report.parked_queue] == [1, 2, 3]
    assert report.parked_queue[0].reason == "after the floor ships"
    assert report.parked_queue[2].seq is None
    assert report.counts.parked == 3
    assert report.dropped == ()
    assert "PARKED QUEUE" in report.text


def test_a_later_claim_unparks_the_ask() -> None:
    rows = (
        f"2026-10-31T09:00:00Z | STATUS | lane=orch | ask={ask_id(8)} | state=parked | reason=no slot | seq=1",
        claim("lane-b", "2026-10-31T10:00:00Z", ask_id(8)),
    )
    report = _run((_prompt(8, "Add the reducer"),), rows)
    entry = _entry(report, 8)
    assert entry.state is EnumAskState.OWNED
    assert entry.park_reason is None
    assert report.parked_queue == ()


def test_ticketed_status_row_makes_the_ask_ticketed() -> None:
    row = f"2026-10-31T09:00:00Z | STATUS | lane=orch | ask={ask_id(9)} | state=ticketed | ticket=OMN-500"
    report = _run((_prompt(9, "Graph the factory"),), (row,))
    entry = _entry(report, 9)
    assert entry.state is EnumAskState.TICKETED
    assert entry.tickets == ("OMN-500",)


def test_closes_ask_needs_evidence_withdrawal_needs_none() -> None:
    no_evidence = f"2026-10-31T09:00:00Z | STATUS | lane=orch | closes-ask={ask_id(10)} | evidence=trust me"
    assert (
        _state(_run((_prompt(10, "Do a thing"),), (no_evidence,)), 10)
        is EnumAskState.DROPPED
    )

    merged = f"2026-10-31T09:00:00Z | STATUS | lane=orch | closes-ask={ask_id(10)} | evidence=omnimarket#4700"
    report = _run((_prompt(10, "Do a thing"),), (merged,))
    assert _state(report, 10) is EnumAskState.DONE
    assert _entry(report, 10).resolution == "closes-ask"

    withdrawn = (
        f"2026-10-31T09:00:00Z | STATUS | lane=operator-capture | closes-ask={ask_id(10)} | "
        'state=dropped | evidence=operator-drop | "drop it"'
    )
    report = _run((_prompt(10, "Do a thing"),), (withdrawn,))
    assert _entry(report, 10).resolution == "withdrawn"
    assert _state(report, 10) is EnumAskState.DONE


def test_hold_parks_and_message_ticket_does_not_own() -> None:
    hold = (
        f"2026-10-31T09:00:00Z | HOLD | lane=orch | id=2026-10-31T09:00:00Z-orch | ask={ask_id(11)} | "
        "to=orch | until=2026-11-01T00:00:00Z | reason=waits for the host"
    )
    assert _state(_run((_prompt(11, "Rack 51"),), (hold,)), 11) is EnumAskState.PARKED
    msg = (
        f"2026-10-31T09:00:00Z | MSG | from=orch | to=other | id=2026-10-31T09:00:00Z-orch | "
        f"ask={ask_id(11)} | ticket=OMN-9"
    )
    assert _state(_run((_prompt(11, "Rack 51"),), (msg,)), 11) is EnumAskState.TICKETED


def test_row_that_predates_the_ask_is_ignored() -> None:
    early = claim("lane-a", "2026-10-31T07:00:00Z", ask_id(12))
    assert (
        _state(_run((_prompt(12, "Late ask"),), (early,)), 12) is EnumAskState.DROPPED
    )


def test_inferred_claim_owns_an_ask_no_row_cites_and_is_marked_inferred() -> None:
    quote = "Make a graphic of the factory floor with the parked queue"
    row = (
        "2026-10-31T08:30:00Z | CLAIM | lane=factory-floor-node-1200 | ticket=OMN-1 | worktree=none | "
        "est ~3 lane-hours; displaces idle; (OMN-1) | Build the factory floor graphic and parked queue"
    )
    report = _run(
        (_prompt(13, quote, subject="factory floor graphic parked queue"),), (row,)
    )
    entry = _entry(report, 13)
    assert entry.state is EnumAskState.OWNED
    assert entry.owner_lane == "factory-floor-node-1200"
    assert entry.link is EnumAskLink.INFERRED
    assert {"facto", "floor", "graph"} <= set(entry.shared_terms)


def test_one_shared_word_does_not_link_a_claim() -> None:
    row = (
        "2026-10-31T08:30:00Z | CLAIM | lane=other-1 | ticket=OMN-1 | worktree=none | "
        "est ~1 lane-hours; displaces idle; (OMN-1) | Check the swap on the host"
    )
    report = _run((_prompt(14, "Check the MSK topic list"),), (row,))
    assert _state(report, 14) is EnumAskState.DROPPED


def test_words_every_claim_carries_do_not_link_asks() -> None:
    rows = tuple(
        f"2026-10-31T08:{m:02d}:00Z | CLAIM | lane=l{m} | ticket=OMN-1 | worktree=none | "
        "est ~1 lane-hours; displaces idle; (OMN-1) | Codex review of something unrelated"
        for m in range(10, 40)
    )
    report = _run((_prompt(15, "Have codex review everything"),), rows)
    assert _state(report, 15) is EnumAskState.DROPPED


def test_ruling_that_quotes_the_ask_closes_it_unless_a_mechanism_is_pending() -> None:
    words = "Make sure to check the marketplace before we build anything new"
    plain = (
        "2026-10-31T09:00:00Z | RULING | lane=orch | ticket=OMN-1 | question=check first | "
        f'"{words}"'
    )
    report = _run((_prompt(16, words),), (plain,))
    assert _entry(report, 16).resolution == "ruling"
    assert _state(report, 16) is EnumAskState.DONE

    pending = plain.replace("ticket=OMN-1", "mechanism-ticket=OMN-40")
    report = _run((_prompt(16, words),), (pending,))
    assert _state(report, 16) is EnumAskState.TICKETED


def test_untracked_kinds_unclassified_prompts_and_old_prompts() -> None:
    old = _prompt(17, "Yesterday ask", said=datetime(2026, 10, 30, 23, tzinfo=UTC))
    decision = _prompt(18, "Go with the hash", kind="decision")
    bare = ModelAskPrompt(
        capture_id="cap-bare",
        session_id="s",
        said_at=SAID,
        text="keep the foreground silent..",
    )
    report = _run((old, decision, bare))
    assert report.asks == ()
    assert [(k.kind, k.count) for k in report.not_tracked] == [("decision", 1)]
    assert [u.capture_id for u in report.unclassified] == ["cap-bare"]
    assert "UNCLASSIFIED PROMPTS (1)" in report.text


def test_ask_known_only_from_its_ledger_row_is_tracked() -> None:
    row = (
        f"2026-10-31T08:00:05Z | STATUS | lane=operator-capture | kind=ask | item=cap-{20:010x} | ask={ask_id(20)} | "
        "state=open | session=s | said=2026-10-31T08:00:00Z | confidence=0.50 | subject=rack host | "
        '"Rack the host tomorrow"'
    )
    report = _run((), (row,))
    entry = _entry(report, 20)
    assert entry.words == "Rack the host tomorrow"
    assert entry.said_at == SAID
    assert entry.state is EnumAskState.DROPPED
    assert entry.link is EnumAskLink.NONE


def test_wip_reports_unknown_within_and_over() -> None:
    prompts = (_prompt(21, "First thing"), _prompt(22, "Second thing"))
    rows = (
        claim("lane-1", "2026-10-31T08:05:00Z", ask_id(21)),
        claim("lane-2", "2026-10-31T08:06:00Z", ask_id(22)),
    )
    assert _run(prompts, rows).wip.status == "unknown"
    assert (
        _run(
            prompts, rows, config=CONFIG.model_copy(update={"wip_limit": 2})
        ).wip.status
        == "within"
    )
    over = _run(prompts, rows, config=CONFIG.model_copy(update={"wip_limit": 1})).wip
    assert over.status == "over"
    assert over.owned_lanes == ("lane-1", "lane-2")


def test_report_is_deterministic_and_round_trips() -> None:
    prompts = (_prompt(23, "Same input"),)
    first = _run(prompts)
    again = _run(prompts)
    assert first == again
    assert ModelAskStateReport.model_validate_json(first.model_dump_json()) == first
    assert first.schema_id == "omni.ask-state/1"


def _statement_prompt(n: int, quote: str, hour: int) -> ModelAskPrompt:
    return _prompt(
        n, quote, kind="decision", said=datetime(2026, 10, 31, hour, tzinfo=UTC)
    )


def test_third_statement_of_a_direction_flags_the_ask_restated() -> None:
    words = "Sequence the tracked items by leverage within the work in progress limit"
    ask = _prompt(30, words, said=datetime(2026, 10, 31, 9, tzinfo=UTC))
    earlier = _statement_prompt(
        31, "Order the tracked items by leverage inside the work in progress limit", 5
    )
    ruling = (
        "2026-10-31T06:00:00Z | RULING | lane=orch | ticket=OMN-1 | question=ordering | "
        '"sequence tracked items by leverage within the work in progress limit"'
    )
    two = _run((ask, earlier))
    assert _entry(two, 30).restated == 2
    assert not _entry(two, 30).restated_flag
    three = _run((ask, earlier), (ruling,))
    entry = _entry(three, 30)
    assert entry.restated == 3
    assert entry.restated_flag
    assert three.restated_asks == (ask_id(30),)
    assert "RESTATED (1)" in three.text
    assert "stated x3 RESTATED" in three.text


def test_two_items_of_one_prompt_are_one_statement_and_unrelated_words_do_not_count() -> (
    None
):
    both = ModelAskPrompt(
        capture_id="cap-both",
        session_id="s",
        said_at=SAID,
        text="x",
        items=(
            ModelAskPromptItem(
                item_id=f"cap-{40:010x}",
                kind="ask",
                quote="Rack the new host tomorrow",
                subject="",
                confidence=0.5,
            ),
            ModelAskPromptItem(
                item_id=f"cap-{41:010x}",
                kind="idea",
                quote="Rack the new host tomorrow morning",
                subject="",
                confidence=0.5,
            ),
        ),
    )
    other = _statement_prompt(42, "Graph the factory floor with animations", 3)
    report = _run((both, other))
    assert _entry(report, 40).restated == 1


def test_leverage_counts_holds_after_the_asks_pr_and_other_lanes_citing_its_ticket() -> (
    None
):
    claim_row = (
        "2026-10-31T08:05:00Z | CLAIM | lane=lane-a | ticket=OMN-77 | ask="
        + ask_id(50)
        + " | pr=omnimarket#900 | "
        "worktree=none | est ~1 lane-hours; displaces nothing; (OMN-77)"
    )
    rows = (
        claim_row,
        "2026-10-31T08:10:00Z | HOLD | lane=lane-b | id=2026-10-31T08:10:00Z-lane-b | pr=omnimarket#901 | after=omnimarket#900 | reason=waits",
        "2026-10-31T08:11:00Z | HOLD | lane=lane-c | id=2026-10-31T08:11:00Z-lane-c | pr=omnimarket#902 | after=omnimarket#900 | reason=waits",
        "2026-10-31T08:12:00Z | FRICTION | lane=lane-d | existing=OMN-77 | cost=~5 lane-minutes | blocked on it",
        "2026-10-31T08:13:00Z | TERMINAL | lane=lane-e | friction=none | outcome=blocked | blocked-by=OMN-77 | delegated=0",
        "2026-10-31T08:14:00Z | HOLD | lane=lane-f | id=2026-10-31T08:14:00Z-lane-f | pr=omnimarket#903 | after=omnimarket#999 | reason=unrelated",
    )
    report = _run((_prompt(50, "Ship the shared parser"),), rows)
    entry = _entry(report, 50)
    assert entry.leverage == 4
    assert "lane-b waits on omnimarket#900" in entry.leverage_reason
    assert "lane-d friction cites OMN-77" in entry.leverage_reason
    lonely = _run((_prompt(51, "Do a standalone thing"),))
    assert _entry(lonely, 51).leverage == 0
    assert "no dependency edge" in _entry(lonely, 51).leverage_reason


def _parked(n: int, seq: int | None) -> str:
    cell = f" | seq={seq}" if seq is not None else ""
    return f"2026-10-31T09:{n - 50:02d}:00Z | STATUS | lane=orch | ask={ask_id(n)} | state=parked | reason=r{n}{cell}"


def test_parked_queue_orders_restated_then_leverage_then_seq_and_marks_wip() -> None:
    words = {
        60: "Rack hardware tomorrow morning",
        61: "Calibrate telemetry exporters nightly",
        62: "Migrate dashboard widgets gradually",
        63: "Consolidate runtime containers aggressively",
    }
    prompts = tuple(
        _prompt(n, text, said=datetime(2026, 10, 31, 1, tzinfo=UTC))
        for n, text in words.items()
    )
    rows = (
        _parked(60, 1),
        _parked(61, 2),
        _parked(62, 3),
        _parked(63, 4),
        "2026-10-31T09:20:00Z | HOLD | lane=lane-b | id=2026-10-31T09:20:00Z-lane-b | pr=omnimarket#901 | after=omnimarket#900 | reason=w",
        f"2026-10-31T09:21:00Z | STATUS | lane=orch | ask={ask_id(62)} | pr=omnimarket#900",
        # ask 63 is restated three times by two more prompts of the same direction
    )
    more = (
        _statement_prompt(70, "Consolidate runtime containers aggressively again", 2),
        _statement_prompt(
            71, "Consolidate runtime containers aggressively once more", 3
        ),
    )
    config = CONFIG.model_copy(update={"wip_limit": 3})
    claims = (
        claim("lane-1", "2026-10-31T09:30:00Z", ask_id(80)),
        claim("lane-2", "2026-10-31T09:31:00Z", ask_id(81)),
    )
    owned = (_prompt(80, "Owned thing one"), _prompt(81, "Owned thing two"))
    report = _run((*prompts, *more, *owned), (*rows, *claims), config=config)
    order = [p.ask_id for p in report.parked_queue]
    assert order == [ask_id(63), ask_id(62), ask_id(60), ask_id(61)]
    assert report.parked_queue[0].restated_flag
    assert report.parked_queue[1].leverage == 1
    # three slots, two lanes owned: one free position
    assert [p.within_wip for p in report.parked_queue] == [True, False, False, False]
    unknown = _run((*prompts, *more), rows)
    assert {p.within_wip for p in unknown.parked_queue} == {None}


# OMN-21713 follow-up: the three undercounts measured on 2026-10-31 (real prompts and row shapes).


def _by_words(report: ModelAskStateReport, words: str) -> ModelAskEntry:
    return next(e for e in report.asks if words in e.words)


def test_status_owned_row_names_the_owner() -> None:
    rows = (
        "2026-10-31T14:27:47Z | STATUS | lane=orchestrator-31d9 | ask="
        + ask_id(1)
        + " | state=owned | owner=factory-floor-host-1255 | ticket=OMN-21682 | "
        "Mock-up delivered, live version is node_factory_floor",
    )
    report = _run(
        (_prompt(1, "Maybe with little animations.", said=SAID),),
        rows,
        now=datetime(2026, 10, 31, 14, 40, tzinfo=UTC),
    )
    entry = _entry(report, 1)
    assert entry.state is EnumAskState.OWNED
    assert entry.owner_lane == "factory-floor-host-1255"
    assert report.wip.owned_lanes == ("factory-floor-host-1255",)


def test_sent_on_behalf_row_closes_the_ask_it_cites() -> None:
    rows = (
        "2026-10-31T02:01:08Z | SENT-ON-BEHALF | lane=orchestrator-31d9 | ask="
        + ask_id(1)
        + " | to=cloud-owner | via Slack DM",
    )
    report = _run((_prompt(1, "And send it.", said=SAID),), rows)
    entry = _entry(report, 1)
    assert entry.state is EnumAskState.DONE
    assert entry.resolution == "answered"


def test_message_row_closes_but_a_pull_request_handoff_does_not() -> None:
    answer = (
        "2026-10-31T08:30:00Z | MSG | from=orchestrator-31d9 | to=operator | ask="
        + ask_id(1)
        + " | Chat reply: items 2 and 4 explained"
    )
    handoff = (
        "2026-10-31T08:30:00Z | MSG | from=lane-a | to=landing-controller | ask="
        + ask_id(2)
        + " | pr=omnimarket#4682 | head=abc | needs=land"
    )
    report = _run(
        (_prompt(1, "Tell me about 2 and 4"), _prompt(2, "Build the ask node")),
        (answer, handoff),
    )
    assert _state(report, 1) is EnumAskState.DONE
    assert _state(report, 2) is not EnumAskState.DONE


def test_done_status_with_a_cited_send_row_closes_the_ask() -> None:
    send = (
        "2026-10-31T09:00:00Z | SENT-ON-BEHALF | lane=orchestrator-31d9 | Slack reply"
    )
    done = (
        "2026-10-31T14:19:08Z | STATUS | lane=orchestrator-31d9 | ask="
        + ask_id(1)
        + " | state=done | evidence=SENT-ON-BEHALF:2026-10-31T09:00:00Z | answered"
    )
    report = _run((_prompt(1, "check MSK, and send."),), (send, done))
    assert _state(report, 1) is EnumAskState.DONE
    assert _entry(report, 1).resolution == "closes-ask"


def test_done_status_citing_a_row_that_does_not_exist_closes_nothing() -> None:
    done = (
        "2026-10-31T14:19:08Z | STATUS | lane=orchestrator-31d9 | ask="
        + ask_id(1)
        + " | state=done | evidence=SENT-ON-BEHALF:2026-10-31T09:00:00Z | answered"
    )
    report = _run((_prompt(1, "check MSK, and send."),), (done,))
    assert _state(report, 1) is EnumAskState.DROPPED


def test_done_status_without_evidence_closes_nothing() -> None:
    done = (
        "2026-10-31T14:19:08Z | STATUS | lane=orchestrator-31d9 | ask="
        + ask_id(1)
        + " | state=done | evidence=slack DM sent earlier | answered"
    )
    report = _run((_prompt(1, "And send it."),), (done,))
    assert _state(report, 1) is EnumAskState.DROPPED


@pytest.mark.parametrize(
    ("kind", "quote"),
    [
        ("status_question", "why do we have 82 containers still?"),
        (
            "status_question",
            "What happened to our plan to reduce the number of runtimes and external other containers?",
        ),
        ("decision", "Everything should be one runtime."),
        ("decision", "We also don't need separate runtime effects containers."),
        ("status_question", "Why are you not sending stuff to the cloud?"),
        ("status_question", "Why are we still running so much on 201?"),
        ("decision", "Don't let the lab sit idle."),
    ],
)
def test_challenge_and_directive_items_of_untracked_kinds_are_asks(
    kind: str, quote: str
) -> None:
    report = _run((_prompt(1, quote, kind=kind),))
    assert report.counts.total == 1
    assert _state(report, 1) is EnumAskState.DROPPED
    assert report.not_tracked == ()


@pytest.mark.parametrize(
    ("kind", "quote"),
    [
        ("status_question", "What else is running in the lab right now?"),
        ("status_question", "Why does it matter?"),
        ("status_question", "What about the cloud?"),
        ("decision", "Yes, I totally agree."),
        ("decision", "Don't worry about racking 51 yet."),
    ],
)
def test_plain_questions_and_decisions_stay_untracked(kind: str, quote: str) -> None:
    report = _run((_prompt(1, quote, kind=kind),))
    assert report.counts.total == 0
    assert report.not_tracked[0].kind == kind


def test_unclassified_prompt_with_an_ask_sentence_becomes_an_ask() -> None:
    prompt = ModelAskPrompt(
        capture_id="3439b6e94feb3a40",
        session_id="sess-1",
        said_at=SAID,
        text="Can we create a web server in the lab to host the factory floor page?",
    )
    rows = (
        "2026-10-31T14:28:14Z | STATUS | lane=orchestrator-31d9 | ask=ask-3439b6e94f | "
        "state=owned | owner=factory-floor-host-1255 | ticket=OMN-17427 | web server",
    )
    report = _run((prompt,), rows)
    assert [e.ask_id for e in report.asks] == ["ask-3439b6e94f"]
    assert report.asks[0].state is EnumAskState.OWNED
    assert report.unclassified == ()


def test_unclassified_prompt_a_row_cites_is_adopted_and_others_stay_listed() -> None:
    cited = ModelAskPrompt(
        capture_id="0fd74581d1ab75d5",
        session_id="sess-1",
        said_at=SAID,
        text="The Gen 9 is coming tomorrow, and we got an NVMe drive.",
    )
    other = ModelAskPrompt(
        capture_id="95b635443dfbf536",
        session_id="sess-1",
        said_at=SAID,
        text="Okay, I'm going to bed.",
    )
    rows = (
        "2026-10-31T14:20:00Z | STATUS | lane=orchestrator-31d9 | ask=ask-0fd74581d1 | "
        "state=parked | reason=hardware arrives 2026-11-01 | seq=2 | Gen9 bring-up",
    )
    report = _run((cited, other), rows)
    assert report.asks[0].ask_id == "ask-0fd74581d1"
    assert report.asks[0].state is EnumAskState.PARKED
    assert [u.capture_id for u in report.unclassified] == ["95b635443dfbf536"]
