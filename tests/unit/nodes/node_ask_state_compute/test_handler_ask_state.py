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
        f"2026-10-31T04:00:00Z | STATUS | lane=orch | ask={ask_id(8)} | state=parked | reason=no slot | seq=1",
        claim("lane-b", "2026-10-31T05:00:00Z", ask_id(8)),
    )
    report = _run((_prompt(8, "Add the reducer"),), rows)
    entry = _entry(report, 8)
    assert entry.state is EnumAskState.OWNED
    assert entry.park_reason is None
    assert report.parked_queue == ()


def test_ticketed_status_row_ticketes_the_ask() -> None:
    row = f"2026-10-31T04:00:00Z | STATUS | lane=orch | ask={ask_id(9)} | state=ticketed | ticket=OMN-500"
    report = _run((_prompt(9, "Graph the factory"),), (row,))
    entry = _entry(report, 9)
    assert entry.state is EnumAskState.TICKETED
    assert entry.tickets == ("OMN-500",)


def test_closes_ask_needs_evidence_withdrawal_needs_none() -> None:
    no_evidence = f"2026-10-31T04:00:00Z | STATUS | lane=orch | closes-ask={ask_id(10)} | evidence=trust me"
    assert (
        _state(_run((_prompt(10, "Do a thing"),), (no_evidence,)), 10)
        is EnumAskState.DROPPED
    )

    merged = f"2026-10-31T04:00:00Z | STATUS | lane=orch | closes-ask={ask_id(10)} | evidence=omnimarket#4700"
    report = _run((_prompt(10, "Do a thing"),), (merged,))
    assert _state(report, 10) is EnumAskState.DONE
    assert _entry(report, 10).resolution == "closes-ask"

    withdrawn = (
        f"2026-10-31T04:00:00Z | STATUS | lane=operator-capture | closes-ask={ask_id(10)} | "
        'state=dropped | evidence=operator-drop | "drop it"'
    )
    report = _run((_prompt(10, "Do a thing"),), (withdrawn,))
    assert _entry(report, 10).resolution == "withdrawn"
    assert _state(report, 10) is EnumAskState.DONE


def test_hold_parks_and_message_ticket_does_not_own() -> None:
    hold = (
        f"2026-10-31T04:00:00Z | HOLD | lane=orch | id=2026-10-31T04:00:00Z-orch | ask={ask_id(11)} | "
        "to=orch | until=2026-11-01T00:00:00Z | reason=waits for the host"
    )
    assert _state(_run((_prompt(11, "Rack 51"),), (hold,)), 11) is EnumAskState.PARKED
    msg = (
        f"2026-10-31T04:00:00Z | MSG | from=orch | to=other | id=2026-10-31T04:00:00Z-orch | "
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
        for m in range(10, 20)
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
