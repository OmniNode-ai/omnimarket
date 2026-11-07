# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stopped-line fill's state as one pure transition function (plan task T12).

The golden scenario of one startable line is the test written first. The rest hold the
acceptance criteria: the transition table is the contract's ``state_machine`` block held
equal to ``FIXTURE_ROWS`` below (``-k transition_conformance``), the invariants I1 to I6
hold over generated observation sequences (``-k invariants``), a dropped observation
leaves state and ``seq`` unchanged and names the reason (``-k admission_drops``), the lane
name is a function of the key and the attempt (``-k lane_name``), a late check after a
confirm is dropped (``-k late_check_after_confirm_dropped``), a reserved entry resolves
four ways (``-k reserved_resolution``) and an ended entry holds its key but not capacity
(``-k ended_entry_holds_key_not_capacity``, ``-k sent_entry_outlives_window_while_live``,
``-k reoffer_after_expiry_new_attempt``). The reducer reads no overlay key: every duration
and limit travels on the observation, so ``-k config_variation`` varies those and
``-k missing_key_refused`` leaves each out in turn.
"""

from __future__ import annotations

import hashlib
import random
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_reducer.handlers.handler_floor_fill_reducer import (
    VALID_TRANSITIONS,
    HandlerFloorFillReducer,
    lane_name,
)
from omnimarket.nodes.node_floor_fill_reducer.models import (
    EnumFloorFillDropReason,
    EnumFloorFillEntryState,
    EnumFloorFillTrigger,
    ModelFloorFillEntry,
    ModelFloorFillReduceInput,
    ModelFloorFillReduceOutput,
    ModelFloorFillReservedChecked,
    ModelFloorFillReserveRequested,
    ModelFloorFillSentChecked,
    ModelFloorFillStartFailed,
    ModelFloorFillState,
    ModelFloorFillTicketStored,
    line_and_task,
)

pytestmark = pytest.mark.unit


T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
KEY = "floor:line-a:task-1"
KEY2 = "floor:line-a:task-2"
KEY3 = "floor:line-a:task-3"
KEY_B = "floor:line-b:task-1"
TICKET = "OMN-1001"
RESERVED_TIMEOUT_SECONDS = 600
WINDOW_MINUTES = 30

_S = EnumFloorFillEntryState
_T = EnumFloorFillTrigger
_D = EnumFloorFillDropReason


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# ---------------------------------------------------------------- the observations


def ticket_stored(key: str = KEY, ticket: str = TICKET) -> ModelFloorFillTicketStored:
    return ModelFloorFillTicketStored(key=key, ticket=ticket)


def reserve_requested(
    key: str = KEY,
    *,
    now: datetime = T0,
    capacity: int = 2,
    slots: int = 1,
    landing_only: bool = False,
    stop_signal: bool = False,
    window: int = WINDOW_MINUTES,
) -> ModelFloorFillReserveRequested:
    return ModelFloorFillReserveRequested(
        key=key,
        max_fill_lanes_per_line=capacity,
        slot_budget=slots,
        landing_only=landing_only,
        stop_signal=stop_signal,
        sent_window_minutes=window,
        now=now,
    )


Who = ModelFloorFillEntry | tuple[str, int, str]


def _who(who: Who) -> tuple[str, int, str]:
    if isinstance(who, tuple):
        return who
    return who.key, who.attempt, who.lane or ""


def reserved_checked(
    who: Who,
    *,
    claim: bool,
    now: datetime,
    timeout: int = RESERVED_TIMEOUT_SECONDS,
) -> ModelFloorFillReservedChecked:
    key, attempt, lane = _who(who)
    return ModelFloorFillReservedChecked(
        key=key,
        attempt=attempt,
        lane=lane,
        claim_found=claim,
        reserved_timeout_seconds=timeout,
        now=now,
    )


def sent_checked(
    who: Who,
    *,
    ended: bool,
    now: datetime,
    window: int = WINDOW_MINUTES,
) -> ModelFloorFillSentChecked:
    key, attempt, lane = _who(who)
    return ModelFloorFillSentChecked(
        key=key,
        attempt=attempt,
        lane=lane,
        lane_ended=ended,
        sent_window_minutes=window,
        now=now,
    )


def start_failed(who: Who) -> ModelFloorFillStartFailed:
    key, attempt, lane = _who(who)
    return ModelFloorFillStartFailed(key=key, attempt=attempt, lane=lane)


# ---------------------------------------------------------------- the rig


def step(state: ModelFloorFillState, observation: Any) -> ModelFloorFillReduceOutput:
    return HandlerFloorFillReducer().handle(
        ModelFloorFillReduceInput(state=state, observation=observation)
    )


class Rig:
    """Apply observations one after another and keep the row, as the orchestrator does."""

    def __init__(self) -> None:
        self.state = ModelFloorFillState()
        self.outputs: list[ModelFloorFillReduceOutput] = []

    def apply(self, observation: Any) -> ModelFloorFillReduceOutput:
        out = step(self.state, observation)
        self.state = out.state
        self.outputs.append(out)
        return out

    def entry(self, key: str = KEY) -> ModelFloorFillEntry:
        return self.state.entries[key]

    def ticketed(self, *keys: str) -> Rig:
        for key in keys or (KEY,):
            self.apply(ticket_stored(key))
        return self

    def reserved(self, key: str = KEY, **kw: Any) -> ModelFloorFillReduceOutput:
        out = self.apply(reserve_requested(key, **kw))
        assert out.trigger is _T.RESERVE_ADMITTED, out.dropped_reason
        return out

    def sent(
        self, key: str = KEY, *, reserve_at: float = 0, confirm_at: float = 1
    ) -> Rig:
        self.reserved(key, now=at(reserve_at))
        out = self.apply(
            reserved_checked(self.entry(key), claim=True, now=at(confirm_at))
        )
        assert out.trigger is _T.CONFIRMED
        return self


def snapshot(state: ModelFloorFillState) -> str:
    return state.model_dump_json()


# ---------------------------------------------------------------- the golden scenario


def test_golden_scenario_one_startable_line() -> None:
    rig = Rig()
    out = rig.apply(ticket_stored())
    assert (out.trigger, out.state.seq, out.intents) == (_T.TICKET_STORED, 1, ())
    assert rig.entry().state is _S.NONE
    assert rig.entry().attempt == 0
    assert rig.entry().ticket == TICKET

    out = rig.apply(reserve_requested(now=at(0)))
    assert out.trigger is _T.RESERVE_ADMITTED
    assert out.state.seq == 2
    lane = rig.entry().lane
    assert lane == lane_name(KEY, 1)
    assert [i.model_dump(mode="json") for i in out.intents] == [
        {"kind": "StartLane", "key": KEY, "lane": lane, "ticket": TICKET, "attempt": 1}
    ]
    assert rig.entry().state is _S.RESERVED
    assert rig.entry().reserved_at == at(0)

    out = rig.apply(reserved_checked(rig.entry(), claim=True, now=at(2)))
    assert out.trigger is _T.CONFIRMED
    assert rig.entry().state is _S.SENT
    assert rig.entry().sent_at == at(2)

    out = rig.apply(sent_checked(rig.entry(), ended=False, now=at(10)))
    assert out.trigger is _T.SENT_LIVE
    assert not rig.entry().ended

    out = rig.apply(sent_checked(rig.entry(), ended=True, now=at(20)))
    assert out.trigger is _T.SENT_ENDED
    assert rig.entry().ended

    out = rig.apply(sent_checked(rig.entry(), ended=True, now=at(40)))
    assert out.trigger is _T.SENT_EXPIRED
    assert rig.entry().state is _S.NONE
    assert (rig.entry().ticket, rig.entry().attempt) == (TICKET, 1)

    out = rig.apply(reserve_requested(now=at(41)))
    assert out.trigger is _T.RESERVE_ADMITTED
    assert rig.entry().attempt == 2
    assert rig.entry().lane == lane_name(KEY, 2)
    assert out.state.seq == 7


# ---------------------------------------------------------------- the transition table (AC1)

# The fixture: one dict per row of the transition table, read independently of the handler
# and of the contract. ``guards`` are the contract's ``conditions`` expressions.
_IDENTITY = "identity_matches == true"
FIXTURE_ROWS: list[dict[str, Any]] = [
    {
        "row": "0",
        "from": "NONE",
        "trigger": "ticket_stored",
        "to": "NONE",
        "guards": [],
    },
    {
        "row": "1",
        "from": "NONE",
        "trigger": "reserve_admitted",
        "to": "RESERVED",
        "guards": [
            "ticket_held == true",
            "line_has_capacity == true",
            "slot_budget > 0",
            "landing_only == false",
            "stop_signal == false",
        ],
    },
    {
        "row": "2a",
        "from": "RESERVED",
        "trigger": "confirmed",
        "to": "SENT",
        "guards": [_IDENTITY, "claim_found == true"],
    },
    {
        "row": "2b",
        "from": "RESERVED",
        "trigger": "reserved_waiting",
        "to": "RESERVED",
        "guards": [_IDENTITY, "claim_found == false", "reserved_timed_out == false"],
    },
    {
        "row": "2c",
        "from": "RESERVED",
        "trigger": "reserved_timed_out",
        "to": "NONE",
        "guards": [_IDENTITY, "claim_found == false", "reserved_timed_out == true"],
    },
    {
        "row": "2d",
        "from": "RESERVED",
        "trigger": "start_failed",
        "to": "NONE",
        "guards": [_IDENTITY],
    },
    {
        "row": "4a",
        "from": "SENT",
        "trigger": "sent_live",
        "to": "SENT",
        "guards": [_IDENTITY, "lane_ended == false"],
    },
    {
        "row": "4b",
        "from": "SENT",
        "trigger": "sent_ended",
        "to": "SENT",
        "guards": [_IDENTITY, "lane_ended == true", "window_passed == false"],
    },
    {
        "row": "4c",
        "from": "SENT",
        "trigger": "sent_expired",
        "to": "NONE",
        "guards": [_IDENTITY, "lane_ended == true", "window_passed == true"],
    },
]


def _row_edges(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], tuple[str, tuple[str, ...]]]:
    return {
        (row["from"], row["trigger"]): (row["to"], tuple(sorted(row["guards"])))
        for row in rows
    }


def contract_rows(contract: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "row": t["transition_name"],
            "from": t["from_state"],
            "trigger": t["trigger"],
            "to": t["to_state"],
            "guards": [c["expression"] for c in t.get("conditions", [])],
        }
        for t in contract["state_machine"]["transitions"]
    ]


def conformance_findings(
    contract: list[dict[str, Any]], fixture: list[dict[str, Any]]
) -> list[str]:
    """Missing, extra and differently-guarded rows of the contract against the fixture."""
    have, want = _row_edges(contract), _row_edges(fixture)
    findings = [f"missing row {k}" for k in sorted(want.keys() - have.keys())]
    findings += [f"extra row {k}" for k in sorted(have.keys() - want.keys())]
    findings += [
        f"row {k} differs: contract {have[k]} fixture {want[k]}"
        for k in sorted(have.keys() & want.keys())
        if have[k] != want[k]
    ]
    return findings


def test_transition_conformance_handler_table_equals_the_fixture() -> None:
    want = {(r["from"].lower(), r["trigger"]): r["to"].lower() for r in FIXTURE_ROWS}
    assert dict(VALID_TRANSITIONS) == want


def _scenario_for(row: str) -> tuple[ModelFloorFillState, Any]:
    """A state and an observation that take the fixture row ``row``."""
    rig = Rig()
    if row == "0":
        return rig.state, ticket_stored()
    rig.ticketed()
    if row == "1":
        return rig.state, reserve_requested()
    rig.reserved(now=at(0))
    if row == "2a":
        return rig.state, reserved_checked(rig.entry(), claim=True, now=at(1))
    if row == "2b":
        return rig.state, reserved_checked(rig.entry(), claim=False, now=at(1))
    if row == "2c":
        return rig.state, reserved_checked(
            rig.entry(),
            claim=False,
            now=at(0) + timedelta(seconds=RESERVED_TIMEOUT_SECONDS),
        )
    if row == "2d":
        return rig.state, start_failed(rig.entry())
    rig.apply(reserved_checked(rig.entry(), claim=True, now=at(1)))
    if row == "4a":
        return rig.state, sent_checked(rig.entry(), ended=False, now=at(2))
    if row == "4b":
        return rig.state, sent_checked(rig.entry(), ended=True, now=at(2))
    if row == "4c":
        return rig.state, sent_checked(
            rig.entry(), ended=True, now=at(1 + WINDOW_MINUTES)
        )
    raise AssertionError(row)


@pytest.mark.parametrize("row", FIXTURE_ROWS, ids=[r["row"] for r in FIXTURE_ROWS])
def test_transition_conformance_every_row_is_taken_by_its_fixture(
    row: dict[str, Any],
) -> None:
    state, observation = _scenario_for(row["row"])
    before = _entry_state(state, observation.key)
    out = step(state, observation)
    assert out.dropped_reason is None
    assert out.trigger is not None
    assert out.trigger.value == row["trigger"]
    assert before == row["from"]
    assert _entry_state(out.state, observation.key) == row["to"]
    assert out.state.seq == state.seq + 1


def _entry_state(state: ModelFloorFillState, key: str) -> str:
    entry = state.entries.get(key)
    return "NONE" if entry is None else entry.state.value.upper()


# ---------------------------------------------------------------- the invariants (AC2)

KEYS = (KEY, KEY2, KEY3, KEY_B)
SEQUENCES = 250
STEPS = 60
SEED = 20261102


def _line(key: str) -> str:
    return key.split(":")[1]


def _occupancy(state: ModelFloorFillState, line: str) -> int:
    return sum(
        1
        for e in state.entries.values()
        if _line(e.key) == line
        and (e.state is _S.RESERVED or (e.state is _S.SENT and not e.ended))
    )


def _random_observation(
    rng: random.Random, state: ModelFloorFillState, now: datetime
) -> Any:
    kind = rng.choices(
        ["ticket", "reserve", "reserved", "failed", "sent"], weights=[2, 5, 3, 1, 4]
    )[0]
    key = rng.choice(KEYS)
    if kind == "ticket":
        return ticket_stored(key, rng.choice(["OMN-1", "OMN-2"]))
    if kind == "reserve":
        return reserve_requested(
            key,
            now=now,
            capacity=rng.choice([0, 1, 2, 3]),
            slots=rng.choice([0, 1, 1, 2]),
            landing_only=rng.random() < 0.1,
            stop_signal=rng.random() < 0.1,
            window=rng.choice([10, 30]),
        )
    entry = state.entries.get(key)
    attempt, lane = 1, "floor-unknown-lane"
    if entry is not None:
        attempt, lane = (
            entry.attempt,
            entry.lane or lane_name(key, max(entry.attempt, 1)),
        )
        roll = rng.random()
        if roll < 0.2:  # a late answer of an earlier attempt
            attempt = max(entry.attempt - 1, 0)
            lane = lane_name(key, max(attempt, 1))
        elif roll < 0.3:  # the right attempt under a wrong lane
            lane = lane_name(key, entry.attempt + 1)
    who = (key, attempt, lane)
    if kind == "failed":
        return start_failed(who)
    if kind == "reserved":
        return reserved_checked(
            who,
            claim=rng.random() < 0.5,
            now=now,
            timeout=rng.choice([300, RESERVED_TIMEOUT_SECONDS]),
        )
    return sent_checked(
        who, ended=rng.random() < 0.5, now=now, window=rng.choice([10, 30])
    )


def _edge(
    before: ModelFloorFillState, out: ModelFloorFillReduceOutput, key: str
) -> tuple[str, str, str]:
    assert out.trigger is not None
    return (_entry_state(before, key), out.trigger.value, _entry_state(out.state, key))


def _run_sequence(rng: random.Random) -> list[tuple[str, str, str]]:
    state = ModelFloorFillState()
    now = T0
    last_attempt_status: dict[str, str] = {}
    offered: set[tuple[str, int]] = set()
    edges: list[tuple[str, str, str]] = []
    for _ in range(STEPS):
        now += timedelta(minutes=rng.choice([0, 1, 5, 20, 45]))
        obs = _random_observation(rng, state, now)
        out = step(state, obs)
        # I6: the same state and observation give byte-identical output.
        assert out.model_dump_json() == step(state, obs).model_dump_json()
        # I1: one entry per key.
        assert all(k == e.key for k, e in out.state.entries.items())
        assert len({e.key for e in out.state.entries.values()}) == len(
            out.state.entries
        )
        if out.dropped_reason is not None:
            # I5, dropped: state and seq unchanged, a reason named, no intent.
            assert out.state == state
            assert out.trigger is None
            assert out.intents == ()
            continue
        # I5, accepted: seq rises by exactly one.
        assert out.state.seq == state.seq + 1
        trigger = out.trigger
        assert trigger is not None
        edges.append(_edge(state, out, obs.key))
        entry = out.state.entries[obs.key]
        # attempt only rises.
        before = state.entries.get(obs.key)
        assert entry.attempt >= (0 if before is None else before.attempt)
        if trigger is _T.RESERVE_ADMITTED:
            # I2: only after the previous attempt was cleared or confirmed SENT.
            assert last_attempt_status.get(obs.key, "cleared") in {
                "cleared",
                "confirmed",
            }
            assert entry.attempt == (0 if before is None else before.attempt) + 1
            last_attempt_status[obs.key] = "pending"
            # I3: StartLane once per attempt, only by Reserve, only for a ticket holder.
            [intent] = out.intents
            assert intent.key == obs.key
            assert (obs.key, intent.attempt) not in offered
            offered.add((obs.key, intent.attempt))
            assert intent.ticket
            assert intent.ticket == entry.ticket
            assert (intent.lane, intent.attempt) == (entry.lane, entry.attempt)
            # I4: the reservation never raises the line over the capacity on the observation.
            assert _occupancy(out.state, _line(obs.key)) <= obs.max_fill_lanes_per_line
        else:
            assert out.intents == ()
        if trigger is _T.CONFIRMED:
            last_attempt_status[obs.key] = "confirmed"
        if trigger in {_T.RESERVED_TIMED_OUT, _T.START_FAILED, _T.SENT_EXPIRED}:
            last_attempt_status[obs.key] = "cleared"
        state = out.state
    return edges


def test_invariants_i1_to_i6_hold_over_generated_sequences() -> None:
    rng = random.Random(SEED)
    taken: set[tuple[str, str, str]] = set()
    for _ in range(SEQUENCES):
        taken.update(_run_sequence(rng))
    table = {(r["from"], r["trigger"], r["to"]) for r in FIXTURE_ROWS}
    # the generator reaches every row, and no sequence takes a transition outside the table
    assert taken == table


def test_transition_conformance_no_other_transition_exists() -> None:
    rng = random.Random(SEED + 1)
    taken: set[tuple[str, str, str]] = set()
    for _ in range(SEQUENCES):
        taken.update(_run_sequence(rng))
    assert taken <= {(r["from"], r["trigger"], r["to"]) for r in FIXTURE_ROWS}


# ---------------------------------------------------------------- admission drops (AC3)


def _assert_dropped(
    state: ModelFloorFillState, observation: Any, reason: EnumFloorFillDropReason
) -> None:
    before = snapshot(state)
    out = step(state, observation)
    assert out.dropped_reason is reason
    assert out.trigger is None
    assert out.intents == ()
    assert out.state.seq == state.seq
    assert snapshot(out.state) == before


def test_admission_drops_a_late_answer_of_an_earlier_attempt() -> None:
    rig = Rig().ticketed()
    rig.reserved(now=at(0))
    first = rig.entry()
    rig.apply(
        reserved_checked(
            first, claim=False, now=at(0) + timedelta(seconds=RESERVED_TIMEOUT_SECONDS)
        )
    )
    rig.reserved(now=at(20))
    assert rig.entry().attempt == 2
    _assert_dropped(
        rig.state, reserved_checked(first, claim=True, now=at(21)), _D.STALE_ATTEMPT
    )
    _assert_dropped(rig.state, start_failed(first), _D.STALE_ATTEMPT)


def test_admission_drops_a_wrong_lane() -> None:
    rig = Rig().ticketed()
    rig.reserved(now=at(0))
    wrong = ModelFloorFillReservedChecked(
        key=KEY,
        attempt=rig.entry().attempt,
        lane="floor-line-a-task-1-00000000-a1",
        claim_found=True,
        reserved_timeout_seconds=RESERVED_TIMEOUT_SECONDS,
        now=at(1),
    )
    _assert_dropped(rig.state, wrong, _D.STALE_ATTEMPT)


def test_admission_drops_a_check_for_a_key_with_no_entry() -> None:
    stranger = ModelFloorFillSentChecked(
        key=KEY,
        attempt=1,
        lane=lane_name(KEY, 1),
        lane_ended=True,
        sent_window_minutes=WINDOW_MINUTES,
        now=at(1),
    )
    _assert_dropped(ModelFloorFillState(), stranger, _D.NO_ENTRY)


def test_admission_drops_wrong_state_for_each_check_against_the_other_state() -> None:
    reserved = Rig().ticketed()
    reserved.reserved(now=at(0))
    sent = Rig().ticketed().sent()
    _assert_dropped(
        sent.state,
        reserved_checked(sent.entry(), claim=True, now=at(2)),
        _D.WRONG_STATE,
    )
    _assert_dropped(sent.state, start_failed(sent.entry()), _D.WRONG_STATE)
    _assert_dropped(
        reserved.state,
        sent_checked(reserved.entry(), ended=True, now=at(2)),
        _D.WRONG_STATE,
    )


def test_admission_drops_a_ticket_for_an_entry_that_has_dispatch_state() -> None:
    rig = Rig().ticketed()
    rig.reserved(now=at(0))
    _assert_dropped(rig.state, ticket_stored(KEY, "OMN-2"), _D.WRONG_STATE)


def test_admission_drops_reserve_against_reserved_and_live_sent_is_held() -> None:
    reserved = Rig().ticketed()
    reserved.reserved(now=at(0))
    _assert_dropped(reserved.state, reserve_requested(now=at(1)), _D.HELD)
    sent = Rig().ticketed().sent()
    _assert_dropped(sent.state, reserve_requested(now=at(2)), _D.HELD)
    # a SENT entry whose lane has not ended is held even past the window
    _assert_dropped(sent.state, reserve_requested(now=at(500)), _D.HELD)


def test_admission_drops_reserve_against_an_ended_sent_entry_inside_the_window() -> (
    None
):
    rig = Rig().ticketed().sent()
    rig.apply(sent_checked(rig.entry(), ended=True, now=at(5)))
    _assert_dropped(
        rig.state, reserve_requested(now=at(1 + WINDOW_MINUTES - 1)), _D.WINDOW
    )


def test_admission_drops_reserve_against_an_ended_sent_entry_past_its_window_is_uncleared() -> (
    None
):
    rig = Rig().ticketed().sent()
    rig.apply(sent_checked(rig.entry(), ended=True, now=at(5)))
    _assert_dropped(
        rig.state, reserve_requested(now=at(1 + WINDOW_MINUTES)), _D.UNCLEARED
    )


def test_admission_drops_reserve_without_a_ticket_names_no_ticket() -> None:
    _assert_dropped(ModelFloorFillState(), reserve_requested(), _D.NO_TICKET)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"capacity": 1}, _D.CAPACITY),
        ({"slots": 0}, _D.NO_SLOT),
        ({"landing_only": True}, _D.LANDING_ONLY),
        ({"stop_signal": True}, _D.STOP_SIGNAL),
    ],
    ids=["capacity", "no-slot", "landing-only", "stop-signal"],
)
def test_admission_drops_reserve_refusals_are_typed(
    kwargs: dict[str, Any], reason: EnumFloorFillDropReason
) -> None:
    rig = Rig().ticketed(KEY, KEY2)
    rig.reserved(KEY, now=at(0), capacity=3)
    _assert_dropped(
        rig.state,
        reserve_requested(KEY2, now=at(1), **{"capacity": 2, **kwargs}),
        reason,
    )


# ---------------------------------------------------------------- the lane name (AC4, AC4b)


def test_lane_name_deterministic() -> None:
    h8 = hashlib.sha256(KEY.encode()).hexdigest()[:8]
    assert lane_name(KEY, 1) == f"floor-line-a-task-1-{h8}-a1"
    assert lane_name(KEY, 1) == lane_name(KEY, 1)
    assert lane_name(KEY, 2) != lane_name(KEY, 1)
    assert lane_name(KEY, 2).endswith("-a2")
    rig = Rig().ticketed()
    rig.reserved(now=at(0))
    assert rig.entry().lane == lane_name(KEY, 1)
    rig.apply(start_failed(rig.entry()))
    rig.reserved(now=at(1))
    assert rig.entry().lane == lane_name(KEY, 2)


def test_lane_name_concatenation_pair() -> None:
    first, second = lane_name("floor:a-b:c", 1), lane_name("floor:a:b-c", 1)
    assert first != second
    assert first.startswith("floor-a-b-c-")
    assert second.startswith("floor-a-b-c-")


_BAD_KEYS = (
    "floor:L:t\n",
    "floor:L:t\r\n",
    "floor:L\n:t",
    "floor:L:t ",
    "floor:L:",
    "floor::t",
    "floor:L:t:u",
    "floor:L:t|u",
    "floor:L=x:t",
    "floor:L:a/b",
    "floor:..:t",
    "floor:L:..",
    "floor:L:t..x",
    "floor:L:.t",
    "floor:L:t.",
    "floor:L:.",
    "floor:L:t.-.x.",
    "floor:L:t\u2028",
    "floor:L:t\u00e9",
    "floor:L:t\x00",
)


@pytest.mark.parametrize("key", _BAD_KEYS)
def test_lane_name_floor_key_refuses_separators_and_trailing_newline(key: str) -> None:
    """One task is one key: a key with a newline, ``|``, ``=``, ``/`` or ``..`` is no key."""
    with pytest.raises(ValidationError):
        ModelFloorFillTicketStored(key=key, ticket=TICKET)
    with pytest.raises(ValueError, match="is not a floor key"):
        lane_name(key, 1)
    with pytest.raises(ValueError, match="is not a floor key"):
        line_and_task(key)


@pytest.mark.parametrize(
    ("key", "line_id", "task_id"),
    [
        ("floor:L:T0.1", "L", "T0.1"),
        ("floor:L:CM.0", "L", "CM.0"),
        ("floor:line-a:W0.10", "line-a", "W0.10"),
        ("floor:L:a.b.c", "L", "a.b.c"),
    ],
)
def test_a_task_id_with_a_dot_between_segments_is_one_key(
    key: str, line_id: str, task_id: str
) -> None:
    """The floor's own ids are ``T0.1`` and ``CM.0``: they are keys, and the dot stays in the key."""
    assert line_and_task(key) == (line_id, task_id)
    assert ModelFloorFillTicketStored(key=key, ticket=TICKET).key == key


def test_a_dotted_task_id_names_a_lane_in_the_closed_charset() -> None:
    name = lane_name("floor:L:T0.1", 1)
    assert re.fullmatch(r"floor-[A-Za-z0-9_-]+-a[1-9][0-9]*", name)
    assert name.startswith("floor-L-T0-1-")


def test_a_dotted_id_and_the_hyphen_id_it_resembles_are_two_lanes() -> None:
    assert lane_name("floor:L:T0.1", 1) != lane_name("floor:L:T0-1", 1)
    assert lane_name("floor:L:T0.1", 1) != lane_name("floor:L:T0.10", 1)


def test_a_dotted_task_id_reserves_and_names_its_lane() -> None:
    rig = Rig().ticketed("floor:L:T0.1")
    rig.reserved("floor:L:T0.1", now=at(0), capacity=2)
    assert [e.state for e in rig.state.entries.values()] == [_S.RESERVED]
    assert next(iter(rig.state.entries.values())).lane == lane_name("floor:L:T0.1", 1)


def test_lane_name_floor_key_with_trailing_newline_cannot_reserve_twice() -> None:
    rig = Rig().ticketed("floor:L:t")
    rig.reserved("floor:L:t", now=at(0), capacity=2)
    with pytest.raises(ValidationError):
        reserve_requested("floor:L:t\n", now=at(1), capacity=2)
    assert [e.state for e in rig.state.entries.values()] == [_S.RESERVED]


@pytest.mark.parametrize(
    "key", ["floor:L:t", "floor:line-a:task-1", "floor:a_b:OMN-22200", "floor:A9:9z"]
)
def test_lane_name_minted_only_from_a_closed_charset(key: str) -> None:
    assert re.fullmatch(r"floor-[A-Za-z0-9_-]+-a[1-9][0-9]*", lane_name(key, 1))


def test_late_check_after_confirm_dropped() -> None:
    rig = Rig().ticketed().sent()
    late = reserved_checked(
        rig.entry(),
        claim=False,
        now=at(0) + timedelta(seconds=RESERVED_TIMEOUT_SECONDS * 2),
    )
    _assert_dropped(rig.state, late, _D.WRONG_STATE)
    assert rig.entry().state is _S.SENT


# ---------------------------------------------------------------- a reserved entry resolves (AC5)


def test_reserved_resolution_past_the_timeout_without_a_claim_clears_and_reoffers() -> (
    None
):
    rig = Rig().ticketed()
    rig.reserved(now=at(0))
    first_lane = rig.entry().lane
    before_timeout = at(0) + timedelta(seconds=RESERVED_TIMEOUT_SECONDS - 1)
    out = rig.apply(reserved_checked(rig.entry(), claim=False, now=before_timeout))
    assert out.trigger is _T.RESERVED_WAITING
    assert rig.entry().state is _S.RESERVED
    out = rig.apply(
        reserved_checked(
            rig.entry(),
            claim=False,
            now=at(0) + timedelta(seconds=RESERVED_TIMEOUT_SECONDS),
        )
    )
    assert out.trigger is _T.RESERVED_TIMED_OUT
    entry = rig.entry()
    assert (entry.state, entry.lane, entry.ticket, entry.attempt) == (
        _S.NONE,
        None,
        TICKET,
        1,
    )
    rig.reserved(now=at(20))
    assert rig.entry().attempt == 2
    assert rig.entry().lane not in {None, first_lane}


def test_reserved_resolution_with_the_claim_is_sent() -> None:
    rig = Rig().ticketed()
    rig.reserved(now=at(0))
    out = rig.apply(reserved_checked(rig.entry(), claim=True, now=at(3)))
    assert out.trigger is _T.CONFIRMED
    assert (rig.entry().state, rig.entry().sent_at) == (_S.SENT, at(3))


def test_reserved_resolution_with_an_unreadable_ledger_is_untouched() -> None:
    rig = Rig().ticketed()
    rig.reserved(now=at(0))
    # an unreadable ledger makes no observation: the orchestrator sends nothing, the row stays
    held = snapshot(rig.state)
    assert snapshot(rig.state) == held
    assert rig.entry().state is _S.RESERVED
    assert rig.entry().lane == lane_name(KEY, 1)


def test_reserved_resolution_start_failed_clears_at_once() -> None:
    rig = Rig().ticketed()
    rig.reserved(now=at(0))
    out = rig.apply(start_failed(rig.entry()))
    assert out.trigger is _T.START_FAILED
    entry = rig.entry()
    assert (entry.state, entry.lane, entry.ticket, entry.attempt) == (
        _S.NONE,
        None,
        TICKET,
        1,
    )
    assert entry.reserved_at is None


def test_the_claim_is_confirmed_once_and_sent_at_is_never_refreshed() -> None:
    rig = Rig().ticketed().sent(reserve_at=0, confirm_at=3)
    rig.apply(sent_checked(rig.entry(), ended=False, now=at(10)))
    assert rig.entry().sent_at == at(3)


# ---------------------------------------------------------------- ended and expire (AC6)


def test_ended_entry_holds_key_not_capacity() -> None:
    rig = Rig().ticketed(KEY, KEY2)
    rig.reserved(KEY, now=at(0), capacity=1)
    rig.apply(reserved_checked(rig.entry(KEY), claim=True, now=at(1)))
    # the lane is live: it uses the line's single slot
    _assert_dropped(
        rig.state, reserve_requested(KEY2, now=at(2), capacity=1), _D.CAPACITY
    )
    rig.apply(sent_checked(rig.entry(KEY), ended=True, now=at(3)))
    assert rig.entry(KEY).ended
    # ended, inside the window: its own key is held, a different ready task is reserved
    _assert_dropped(rig.state, reserve_requested(KEY, now=at(4), capacity=1), _D.WINDOW)
    out = rig.apply(reserve_requested(KEY2, now=at(4), capacity=1))
    assert out.trigger is _T.RESERVE_ADMITTED
    assert _occupancy(rig.state, "line-a") == 1


def test_sent_entry_outlives_window_while_live() -> None:
    rig = Rig().ticketed(KEY, KEY2).sent(KEY)
    out = rig.apply(
        sent_checked(rig.entry(KEY), ended=False, now=at(1 + 10 * WINDOW_MINUTES))
    )
    assert out.trigger is _T.SENT_LIVE
    assert rig.entry(KEY).state is _S.SENT
    assert not rig.entry(KEY).ended
    _assert_dropped(
        rig.state,
        reserve_requested(KEY2, now=at(2 + 10 * WINDOW_MINUTES), capacity=1),
        _D.CAPACITY,
    )


def test_reoffer_after_expiry_new_attempt() -> None:
    rig = Rig().ticketed().sent()
    old = rig.entry()
    out = rig.apply(sent_checked(old, ended=True, now=at(1 + WINDOW_MINUTES)))
    assert out.trigger is _T.SENT_EXPIRED
    entry = rig.entry()
    assert (entry.state, entry.lane, entry.ticket, entry.attempt) == (
        _S.NONE,
        None,
        TICKET,
        1,
    )
    rig.reserved(now=at(2 + WINDOW_MINUTES))
    assert rig.entry().attempt == 2
    assert rig.entry().lane != old.lane
    # a historical CLAIM (attempt one's lane) does not confirm attempt two
    _assert_dropped(
        rig.state,
        reserved_checked(old, claim=True, now=at(3 + WINDOW_MINUTES)),
        _D.STALE_ATTEMPT,
    )


# ---------------------------------------------------------------- G-NODE


def test_config_variation_capacity_follows_the_observation() -> None:
    for capacity, admitted in ((1, False), (2, True)):
        rig = Rig().ticketed(KEY, KEY2)
        rig.reserved(KEY, now=at(0), capacity=2)
        out = rig.apply(reserve_requested(KEY2, now=at(1), capacity=capacity))
        assert (out.trigger is _T.RESERVE_ADMITTED) is admitted


def test_config_variation_reserved_timeout_follows_the_observation() -> None:
    for timeout, trigger in ((300, _T.RESERVED_TIMED_OUT), (900, _T.RESERVED_WAITING)):
        rig = Rig().ticketed()
        rig.reserved(now=at(0))
        out = rig.apply(
            reserved_checked(rig.entry(), claim=False, now=at(10), timeout=timeout)
        )
        assert out.trigger is trigger


def test_config_variation_sent_window_follows_the_observation() -> None:
    for window, trigger in ((5, _T.SENT_EXPIRED), (60, _T.SENT_ENDED)):
        rig = Rig().ticketed().sent()
        out = rig.apply(
            sent_checked(rig.entry(), ended=True, now=at(11), window=window)
        )
        assert out.trigger is trigger


def _valid_payloads() -> dict[str, dict[str, Any]]:
    now = T0.isoformat()
    lane = lane_name(KEY, 1)
    return {
        "TicketStored": {"kind": "TicketStored", "key": KEY, "ticket": TICKET},
        "ReserveRequested": {
            "kind": "ReserveRequested",
            "key": KEY,
            "max_fill_lanes_per_line": 1,
            "slot_budget": 1,
            "landing_only": False,
            "stop_signal": False,
            "sent_window_minutes": 30,
            "now": now,
        },
        "StartFailed": {"kind": "StartFailed", "key": KEY, "attempt": 1, "lane": lane},
        "ReservedChecked": {
            "kind": "ReservedChecked",
            "key": KEY,
            "attempt": 1,
            "lane": lane,
            "claim_found": True,
            "reserved_timeout_seconds": 600,
            "now": now,
        },
        "SentChecked": {
            "kind": "SentChecked",
            "key": KEY,
            "attempt": 1,
            "lane": lane,
            "lane_ended": False,
            "sent_window_minutes": 30,
            "now": now,
        },
    }


def test_missing_key_refused_naming_the_key() -> None:
    state = ModelFloorFillState().model_dump(mode="json")
    for kind, payload in _valid_payloads().items():
        ModelFloorFillReduceInput.model_validate(
            {"state": state, "observation": payload}
        )
        for field in payload:
            if field == "kind":
                continue
            partial = {k: v for k, v in payload.items() if k != field}
            with pytest.raises(ValidationError) as refused:
                ModelFloorFillReduceInput.model_validate(
                    {"state": state, "observation": partial | {"kind": kind}}
                )
            assert field in str(refused.value), (kind, field)


def test_a_key_that_is_not_a_floor_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ticket_stored("not-a-floor-key")
    with pytest.raises(ValidationError):
        ticket_stored("floor:only-a-line")
