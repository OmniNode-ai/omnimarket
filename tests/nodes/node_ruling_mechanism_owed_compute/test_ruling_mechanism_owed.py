# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A mechanism ticket whose lane ended unfinished, with no CLAIM after one tick, is owed.

Recorded case: lane one-runtime-fold-1245 ended blocked on 2026-10-24T12:52:12Z on the
ticket a process ruling re-pointed to, and no lane picked it up for six days.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_ruling_mechanism_owed_compute.handlers import (
    handler_ruling_mechanism_owed,
)
from omnimarket.nodes.node_ruling_mechanism_owed_compute.handlers.handler_ruling_mechanism_owed import (
    HandlerRulingMechanismOwed,
)
from omnimarket.nodes.node_ruling_mechanism_owed_compute.models.model_ruling_mechanism_owed import (
    ModelRulingMechanismOwedRequest,
    ModelRulingMechanismOwedResult,
)
from omnimarket.nodes.node_ruling_mechanism_owed_compute.node import (
    NodeRulingMechanismOwedCompute,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_slice_2026_10_24.txt"
RECORDED_TICK = "2026-10-24T13:30:00Z"
FOLD_KEY = "mechanism-owed:OMN-21288:2026-10-24T12:52:12Z"
LAND_KEY = "mechanism-owed:OMN-18594:2026-10-24T12:06:44Z"


def slice_rows() -> tuple[str, ...]:
    return tuple(
        line
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def compute(
    rows: tuple[str, ...] | list[str], tick: str, interval: int = 1800
) -> ModelRulingMechanismOwedResult:
    return HandlerRulingMechanismOwed().handle(
        ModelRulingMechanismOwedRequest(
            rows=tuple(rows), tick_at=tick, tick_interval_seconds=interval
        )
    )


def keys(result: ModelRulingMechanismOwedResult) -> list[str]:
    return [row.key for row in result.owed]


def ledger(
    terminal: str, *extra: str, ruling: str = "mechanism-ticket=OMN-100"
) -> list[str]:
    return [
        f"2026-10-20T10:00:00Z | RULING | lane=orchestrator | kind=process | {ruling}",
        "2026-10-24T10:00:00Z | CLAIM | lane=build | ticket=OMN-100",
        f"2026-10-24T11:00:00Z | TERMINAL | lane=build | ticket=OMN-100 | {terminal}",
        *extra,
    ]


# ---- AC1: the recorded slice of 2026-10-24 ----


def test_recorded_slice_blocked_terminal_yields_one_owed_row() -> None:
    result = compute(slice_rows(), RECORDED_TICK)
    assert keys(result) == [LAND_KEY, FOLD_KEY]
    fold = next(row for row in result.owed if row.key == FOLD_KEY)
    assert (fold.ticket, fold.terminal_at) == ("OMN-21288", "2026-10-24T12:52:12Z")
    assert (fold.lane, fold.outcome) == ("one-runtime-fold-1245", "blocked")
    # The ruling reached the ticket through its CORRECTION, not its original cell.
    assert fold.rulings == ("2026-10-20T21:56:19Z",)


def test_recorded_slice_one_row_per_terminal_stamp_and_ticket() -> None:
    result = compute(slice_rows(), RECORDED_TICK)
    assert len(set(keys(result))) == len(result.owed) == 2


def test_recorded_slice_unfinished_terminal_with_a_later_claim_is_not_owed() -> None:
    # Four unfinished TERMINALs on OMN-17427 sit in the slice; a lane claimed it after each.
    rows = slice_rows()
    unfinished = [
        r
        for r in rows
        if " | TERMINAL | " in r
        and "ticket=OMN-17427" in r
        and re.search(r"outcome=(blocked|failed|partial)", r)
    ]
    assert len(unfinished) == 4
    assert not [k for k in keys(compute(rows, RECORDED_TICK)) if "OMN-17427" in k]


def test_recorded_slice_unfinished_terminal_on_unnamed_ticket_is_not_owed() -> None:
    rows = slice_rows()
    unnamed = [r for r in rows if "ticket=OMN-21155" in r and "outcome=blocked" in r]
    assert unnamed, (
        "positive control: the slice holds a blocked TERMINAL on a ticket no ruling names"
    )
    assert not [k for k in keys(compute(rows, RECORDED_TICK)) if "OMN-21155" in k]


def test_a_claim_after_the_tick_is_not_read_so_the_replay_stays_fixed() -> None:
    rows = slice_rows()
    claim = (
        "2026-10-24T15:26:32Z | CLAIM | lane=fold-step1-push-1445 | ticket=OMN-21288"
    )
    assert any(r.startswith(claim) for r in rows)
    assert FOLD_KEY in keys(compute(rows, RECORDED_TICK))
    assert FOLD_KEY not in keys(compute(rows, "2026-10-24T15:30:00Z"))


@pytest.mark.parametrize("outcome", ["blocked", "failed", "partial"])
def test_each_unfinished_outcome_yields_one_owed_row(outcome: str) -> None:
    result = compute(ledger(f"outcome={outcome}"), "2026-10-24T12:00:00Z")
    assert keys(result) == ["mechanism-owed:OMN-100:2026-10-24T11:00:00Z"]
    assert result.owed[0].outcome == outcome


def test_a_claim_after_the_terminal_supersedes_it() -> None:
    later = "2026-10-24T11:20:00Z | CLAIM | lane=again | ticket=OMN-100"
    assert not compute(ledger("outcome=blocked", later), "2026-10-24T12:00:00Z").owed


def test_a_claim_on_another_ticket_does_not_supersede() -> None:
    other = "2026-10-24T11:20:00Z | CLAIM | lane=again | ticket=OMN-101"
    assert compute(ledger("outcome=blocked", other), "2026-10-24T12:00:00Z").owed


def test_a_claim_before_the_terminal_does_not_supersede_it() -> None:
    # An orphaned CLAIM that never closed must not hide an abandoned ticket.
    orphan = "2026-10-23T09:00:00Z | CLAIM | lane=orphan | ticket=OMN-100"
    assert compute(ledger("outcome=blocked", orphan), "2026-10-24T12:00:00Z").owed


def test_two_unfinished_terminals_on_one_ticket_are_two_rows() -> None:
    second = (
        "2026-10-24T11:30:00Z | TERMINAL | lane=other | ticket=OMN-100 | outcome=failed"
    )
    result = compute(ledger("outcome=blocked", second), "2026-10-24T13:00:00Z")
    assert keys(result) == [
        "mechanism-owed:OMN-100:2026-10-24T11:00:00Z",
        "mechanism-owed:OMN-100:2026-10-24T11:30:00Z",
    ]


def test_the_same_row_twice_is_one_owed_row() -> None:
    rows = ledger("outcome=blocked")
    assert len(compute(rows + rows, "2026-10-24T12:00:00Z").owed) == 1


def test_a_correction_replaces_the_mechanism_ticket_of_its_ruling() -> None:
    correction = "2026-10-21T10:00:00Z | CORRECTION | lane=fix | corrects=2026-10-20T10:00:00Z | mechanism-ticket=OMN-200"
    moved = compute(ledger("outcome=blocked", correction), "2026-10-24T12:00:00Z")
    assert not moved.owed, "the ruling no longer names OMN-100"
    on_new = [
        *ledger("outcome=blocked", correction),
        "2026-10-24T11:05:00Z | TERMINAL | lane=b2 | ticket=OMN-200 | outcome=blocked",
    ]
    assert keys(compute(on_new, "2026-10-24T12:00:00Z")) == [
        "mechanism-owed:OMN-200:2026-10-24T11:05:00Z"
    ]


def test_a_ticket_no_ruling_names_is_never_owed() -> None:
    rows = ledger("outcome=blocked", ruling="kind=decision")
    assert not compute(rows, "2026-10-24T12:00:00Z").owed


# ---- one tick ----


def test_a_terminal_younger_than_one_tick_is_awaiting_not_owed() -> None:
    rows = ledger("outcome=blocked")
    young = compute(rows, "2026-10-24T11:29:59Z")
    assert not young.owed
    assert young.awaiting_tick == ("mechanism-owed:OMN-100:2026-10-24T11:00:00Z",)
    assert keys(compute(rows, "2026-10-24T11:30:00Z")) == [
        "mechanism-owed:OMN-100:2026-10-24T11:00:00Z"
    ]


def test_the_tick_interval_is_an_input() -> None:
    rows = ledger("outcome=blocked")
    assert not compute(rows, "2026-10-24T11:30:00Z", interval=3600).owed
    assert compute(rows, "2026-10-24T12:00:00Z", interval=3600).owed


# ---- AC2: handed-off ----


def test_handed_off_terminal_yields_no_owed_row() -> None:
    handed = compute(ledger("outcome=handed-off"), "2026-10-24T13:00:00Z")
    assert not handed.owed
    assert not handed.awaiting_tick
    assert compute(ledger("outcome=blocked"), "2026-10-24T13:00:00Z").owed, (
        "positive control: the same ledger with a blocked TERMINAL owes a row"
    )


def test_handed_off_terminals_in_the_recorded_slice_yield_no_owed_row() -> None:
    rows = slice_rows()
    handed = [
        r
        for r in rows
        if " | TERMINAL | " in r
        and "outcome=handed-off" in r
        and "ticket=OMN-19998" in r
    ]
    assert len(handed) > 10, (
        "positive control: many handed-off TERMINALs on a named ticket"
    )
    result = compute(rows, RECORDED_TICK)
    assert result.mechanism_ticket_count > 0
    assert not [row for row in result.owed if row.outcome == "handed-off"]
    assert not [k for k in keys(result) if "OMN-19998" in k]


@pytest.mark.parametrize("outcome", ["done", "skipped-owned", "superseded"])
def test_a_finished_terminal_yields_no_owed_row(outcome: str) -> None:
    assert not compute(ledger(f"outcome={outcome}"), "2026-10-24T13:00:00Z").owed


# ---- replay ----


def test_replay_is_identical_for_any_row_order() -> None:
    rows = list(slice_rows())
    first = compute(rows, RECORDED_TICK)
    random.Random(7).shuffle(rows)
    assert compute(rows, RECORDED_TICK) == first
    assert compute(tuple(reversed(rows)), RECORDED_TICK).model_dump_json() == (
        first.model_dump_json()
    )


def test_handler_reads_no_clock() -> None:
    source = Path(handler_ruling_mechanism_owed.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"\.now\(|utcnow|\.today\(|time\.time|monotonic|import time\b", source
    )


def test_node_entrypoint_delegates_to_the_handler() -> None:
    request = ModelRulingMechanismOwedRequest(rows=slice_rows(), tick_at=RECORDED_TICK)
    assert NodeRulingMechanismOwedCompute().handle(request) == (
        HandlerRulingMechanismOwed().handle(request)
    )


def test_request_refuses_a_malformed_tick() -> None:
    with pytest.raises(ValueError, match="tick_at"):
        ModelRulingMechanismOwedRequest(rows=(), tick_at="2026-10-24 13:30")


# ---- the fixture ----


def test_fixture_is_scrubbed_of_secrets_paths_and_free_text() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    assert not re.search(
        r"(ghp_|github_pat_|sk-[A-Za-z0-9]|xox[abp]-|AKIA|eyJ[A-Za-z0-9]|Bearer |password|secret|token)",
        text,
        re.IGNORECASE,
    )
    for fragment in ("/" + "Users/", "http", "@"):
        assert fragment not in text
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert line.endswith("| text=scrubbed"), line
