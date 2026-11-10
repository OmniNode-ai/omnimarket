# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFloorFillReducer: the stopped-line fill's state as one pure transition function.

Definition-B: ``handle(ModelFloorFillReduceInput) -> ModelFloorFillReduceOutput``. The
row (``seq`` and the entries by key) and one observation come in; the next row, the
trigger taken or the reason the observation was dropped, and the intents come out. No
clock (time and every duration or limit come on the observation), no I/O, no model.

The rules, each a row of the contract's ``state_machine`` block and of the conformance
fixture in the tests:

0. ``TicketStored`` creates the entry, or sets ``ticket`` on an entry with no dispatch
   state; it is the only way an entry gets a ticket.
1. ``ReserveRequested`` is admitted for an entry that has a ticket and no dispatch state
   when the line has capacity (RESERVED and not-ended SENT entries counted), the slot
   budget is above zero and neither the open-count brake nor a stop signal holds. It sets
   RESERVED, adds one to ``attempt``, names the lane and returns ``StartLane``.
2. ``ReservedChecked`` with the CLAIM found makes the entry SENT; without one, at
   ``reserved_at + reserved_timeout_seconds`` or later, the entry loses its dispatch state.
   ``StartFailed`` clears it at once.
4. ``SentChecked`` sets ``ended`` when the lane ended; once the window has passed and the
   lane has ended the entry loses its dispatch state. A SENT entry whose lane has not
   ended stays whole.

A response whose ``(key, attempt, lane)`` do not equal the entry's is dropped
(``stale-attempt``), and a response for an entry in the wrong state is dropped
(``wrong-state``); a refused reserve is dropped with a typed reason. A dropped
observation returns the row unchanged, ``seq`` included. ``seq`` rises by exactly one per
accepted observation.

The next dispatch state of every accepted observation is read from ``VALID_TRANSITIONS``,
the table the contract's ``fsm_handler_binding`` holds equal to its ``state_machine``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from omnimarket.models.floor_fill.enum_floor_fill_drop_reason import (
    EnumFloorFillDropReason,
)
from omnimarket.models.floor_fill.enum_floor_fill_entry_state import (
    EnumFloorFillEntryState,
)
from omnimarket.models.floor_fill.enum_floor_fill_trigger import (
    EnumFloorFillTrigger,
)
from omnimarket.models.floor_fill.model_floor_fill_intent import (
    ModelFloorFillIntent,
)
from omnimarket.models.floor_fill.model_floor_fill_observation import (
    ModelFloorFillReservedChecked,
    ModelFloorFillReserveRequested,
    ModelFloorFillSentChecked,
    ModelFloorFillStartFailed,
    ModelFloorFillTicketStored,
)
from omnimarket.models.floor_fill.model_floor_fill_reduce_input import (
    ModelFloorFillReduceInput,
)
from omnimarket.models.floor_fill.model_floor_fill_reduce_output import (
    ModelFloorFillReduceOutput,
)
from omnimarket.models.floor_fill.model_floor_fill_state import (
    ModelFloorFillEntry,
    ModelFloorFillState,
    line_and_task,
)

_S = EnumFloorFillEntryState
_T = EnumFloorFillTrigger
_D = EnumFloorFillDropReason

# (from_state, trigger) -> to_state. Held equal to the contract's state_machine by
# ``fsm_handler_binding`` (check-fsm-handler-drift) and by the conformance fixture.
VALID_TRANSITIONS: Final = {
    ("none", "ticket_stored"): "none",
    ("none", "reserve_admitted"): "reserved",
    ("reserved", "confirmed"): "sent",
    ("reserved", "reserved_waiting"): "reserved",
    ("reserved", "reserved_timed_out"): "none",
    ("reserved", "start_failed"): "none",
    ("sent", "sent_live"): "sent",
    ("sent", "sent_ended"): "sent",
    ("sent", "sent_expired"): "none",
}

_STATE_BY_NAME: Final = {s.value.lower(): s for s in _S}


def lane_name(key: str, attempt: int) -> str:
    """``floor-<line_id>-<task_id>-<h8>-a<attempt>``: a function of the key and the count.

    ``<h8>`` is the first eight hex digits of the sha256 of the key, which tells apart the
    keys whose ids concatenate alike (``a-b`` + ``c`` and ``a`` + ``b-c``) and a dotted id (``W0.1``) from its
    dashed twin (``W0-1``): the dot of an id is written as ``-`` in the name.
    """
    line_id, task_id = line_and_task(key)
    h8 = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"floor-{line_id}-{task_id}-{h8}-a{attempt}".replace(".", "-")


@dataclass(frozen=True)
class _Drop:
    reason: EnumFloorFillDropReason


@dataclass(frozen=True)
class _Take:
    trigger: EnumFloorFillTrigger
    entry: ModelFloorFillEntry
    intents: tuple[ModelFloorFillIntent, ...] = ()


class HandlerFloorFillReducer:
    """Maps (row, observation) to the next row, the trigger taken and the intents."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["REDUCER"]:
        return "REDUCER"

    def handle(self, request: ModelFloorFillReduceInput) -> ModelFloorFillReduceOutput:
        state, obs = request.state, request.observation
        decision = _decide(state, obs)
        if isinstance(decision, _Drop):
            return ModelFloorFillReduceOutput(
                state=state, dropped_reason=decision.reason
            )
        entries = {**state.entries, decision.entry.key: decision.entry}
        return ModelFloorFillReduceOutput(
            state=ModelFloorFillState(seq=state.seq + 1, entries=entries),
            trigger=decision.trigger,
            intents=decision.intents,
        )


def _decide(
    state: ModelFloorFillState,
    obs: ModelFloorFillTicketStored
    | ModelFloorFillReserveRequested
    | ModelFloorFillStartFailed
    | ModelFloorFillReservedChecked
    | ModelFloorFillSentChecked,
) -> _Drop | _Take:
    entry = state.entries.get(obs.key)
    if isinstance(obs, ModelFloorFillTicketStored):
        return _ticket_stored(entry, obs)
    if isinstance(obs, ModelFloorFillReserveRequested):
        return _reserve(state, entry, obs)
    if entry is None:
        return _Drop(_D.NO_ENTRY)
    if (obs.attempt, obs.lane) != (entry.attempt, entry.lane):
        return _Drop(_D.STALE_ATTEMPT)
    if isinstance(obs, ModelFloorFillSentChecked):
        return _sent_checked(entry, obs)
    if entry.state is not _S.RESERVED:
        return _Drop(_D.WRONG_STATE)
    if isinstance(obs, ModelFloorFillStartFailed):
        return _take(_T.START_FAILED, entry, _cleared(entry))
    return _reserved_checked(entry, obs)


def _take(
    trigger: EnumFloorFillTrigger,
    before: ModelFloorFillEntry,
    after: ModelFloorFillEntry,
    intents: tuple[ModelFloorFillIntent, ...] = (),
) -> _Take:
    """The entry after the trigger, its dispatch state read from the transition table."""
    to_state = _STATE_BY_NAME[
        VALID_TRANSITIONS[(before.state.value.lower(), trigger.value)]
    ]
    return _Take(trigger, after.model_copy(update={"state": to_state}), intents)


def _cleared(entry: ModelFloorFillEntry) -> ModelFloorFillEntry:
    """The entry loses its dispatch state; the ticket and the attempt stay."""
    return entry.model_copy(
        update={"lane": None, "ended": False, "reserved_at": None, "sent_at": None}
    )


def _ticket_stored(
    entry: ModelFloorFillEntry | None, obs: ModelFloorFillTicketStored
) -> _Drop | _Take:
    if entry is None:
        entry = ModelFloorFillEntry(key=obs.key)
    elif entry.state is not _S.NONE:
        return _Drop(_D.WRONG_STATE)
    return _take(
        _T.TICKET_STORED, entry, entry.model_copy(update={"ticket": obs.ticket})
    )


def _window_passed(
    entry: ModelFloorFillEntry, window_minutes: int, now: datetime
) -> bool:
    assert entry.sent_at is not None
    return now >= entry.sent_at + timedelta(minutes=window_minutes)


def _occupancy(state: ModelFloorFillState, line_id: str) -> int:
    """RESERVED and not-ended SENT entries of the line."""
    return sum(
        1
        for e in state.entries.values()
        if line_and_task(e.key)[0] == line_id
        and (e.state is _S.RESERVED or (e.state is _S.SENT and not e.ended))
    )


def _reserve(
    state: ModelFloorFillState,
    entry: ModelFloorFillEntry | None,
    obs: ModelFloorFillReserveRequested,
) -> _Drop | _Take:
    if entry is None or entry.ticket is None:
        return _Drop(_D.NO_TICKET)
    if entry.state is _S.RESERVED or (entry.state is _S.SENT and not entry.ended):
        return _Drop(_D.HELD)
    if entry.state is _S.SENT:
        if _window_passed(entry, obs.sent_window_minutes, obs.now):
            return _Drop(_D.UNCLEARED)
        return _Drop(_D.WINDOW)
    if _occupancy(state, line_and_task(obs.key)[0]) >= obs.max_fill_lanes_per_line:
        return _Drop(_D.CAPACITY)
    if obs.slot_budget <= 0:
        return _Drop(_D.NO_SLOT)
    if obs.landing_only:
        return _Drop(_D.LANDING_ONLY)
    if obs.stop_signal:
        return _Drop(_D.STOP_SIGNAL)
    attempt = entry.attempt + 1
    lane = lane_name(obs.key, attempt)
    reserved = entry.model_copy(
        update={
            "attempt": attempt,
            "lane": lane,
            "ended": False,
            "reserved_at": obs.now,
        }
    )
    intent = ModelFloorFillIntent(
        key=obs.key, lane=lane, ticket=entry.ticket, attempt=attempt
    )
    return _take(_T.RESERVE_ADMITTED, entry, reserved, (intent,))


def _reserved_checked(
    entry: ModelFloorFillEntry, obs: ModelFloorFillReservedChecked
) -> _Take:
    if obs.claim_found:
        return _take(
            _T.CONFIRMED,
            entry,
            entry.model_copy(update={"sent_at": obs.now, "reserved_at": None}),
        )
    assert entry.reserved_at is not None
    if obs.now >= entry.reserved_at + timedelta(seconds=obs.reserved_timeout_seconds):
        return _take(_T.RESERVED_TIMED_OUT, entry, _cleared(entry))
    return _take(_T.RESERVED_WAITING, entry, entry)


def _sent_checked(
    entry: ModelFloorFillEntry, obs: ModelFloorFillSentChecked
) -> _Drop | _Take:
    if entry.state is not _S.SENT:
        return _Drop(_D.WRONG_STATE)
    if not obs.lane_ended:
        return _take(_T.SENT_LIVE, entry, entry)
    if _window_passed(entry, obs.sent_window_minutes, obs.now):
        return _take(_T.SENT_EXPIRED, entry, _cleared(entry))
    return _take(_T.SENT_ENDED, entry, entry.model_copy(update={"ended": True}))
