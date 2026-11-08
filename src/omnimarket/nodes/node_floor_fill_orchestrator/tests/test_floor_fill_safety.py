# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One writer, fail closed, retry after a crash, one lane per key (plan T13 acceptance criteria 3, 4, 5, 9, 10b)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    FloorFillConfigError,
    ModelFloorFillAnswer,
    load_floor_fill_config,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelLaneStart,
    ModelMintResult,
    ModelStateRead,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedFacts,
    ScriptedLedger,
    ScriptedPool,
    ScriptedStarter,
    ScriptedTickets,
    floor_record,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    LINES4,
    OVERLAY,
    T0,
    CountingStore,
    CrashAfterWrites,
    SimulatedCrash,
    SynchronizedSnapshot,
    harness,
    overlay,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support_sqlite_store import (
    SqliteFloorStateStore,
)

pytestmark = pytest.mark.unit

TICK = EnumFloorFillCommand.TICK
ONE_TASK = {"alpha": ["a1"]}


def _sqlite(tmp_path: Path) -> SqliteFloorStateStore:
    return SqliteFloorStateStore(str(tmp_path / "fill.db"), busy_timeout_seconds=5)


# --------------------------------------------------------------------------- one writer (AC3, AC10b)


def _two_commands(
    tmp_path: Path,
    first: tuple[EnumFloorFillCommand, str | None],
    second: tuple[EnumFloorFillCommand, str | None],
) -> tuple[list[ModelFloorFillAnswer], list[ScriptedStarter], CountingStore]:
    real = CountingStore(_sqlite(tmp_path))
    starters = [ScriptedStarter(), ScriptedStarter()]
    shared = SynchronizedSnapshot(real, OVERLAY["scope"], 2)
    answers: list[ModelFloorFillAnswer | None] = [None, None]
    orchestrators = []
    for starter in starters:
        h = harness(lines=ONE_TASK, store=shared, starter=starter)
        orchestrators.append(h.orchestrator())

    def go(index: int, command: tuple[EnumFloorFillCommand, str | None]) -> None:
        answers[index] = orchestrators[index].run(*command)

    threads = [
        threading.Thread(target=go, args=(0, first)),
        threading.Thread(target=go, args=(1, second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert all(a is not None for a in answers)
    return [a for a in answers if a is not None], starters, real


def test_one_writer_two_instances(tmp_path: Path) -> None:
    answers, starters, store = _two_commands(tmp_path, (TICK, None), (TICK, None))
    assert sum(len(s.starts) for s in starters) == 1
    stale = [a for a in answers if a.lines == ("STALE-STATE",)]
    assert len(stale) == 1
    assert stale[0].status is EnumFloorFillStatus.FAILED
    assert stale[0].exit_code != 0
    # The loser's first stale write was its first write: it started nothing after it.
    assert stale[0].key_results == ()
    # First tick on a store with no row creates the row, and the racing insert lost (AC10b).
    assert (
        store.writes.count(OVERLAY["scope"]) == 2
    )  # ticket stored, reserved: the winner's two


def test_send_key_one_writer(tmp_path: Path) -> None:
    answers, starters, _ = _two_commands(
        tmp_path, (TICK, None), (EnumFloorFillCommand.SEND_KEY, "floor:alpha:a1")
    )
    assert sum(len(s.starts) for s in starters) == 1
    assert sum(1 for a in answers if a.lines == ("STALE-STATE",)) == 1


def test_first_tick_creates_row(tmp_path: Path) -> None:
    store = _sqlite(tmp_path)
    assert store.load(OVERLAY["scope"]) == ModelStateRead(payload=None, version=0)
    h = harness(store=store)
    answer = h.orchestrator().run(TICK)
    row = store.load(OVERLAY["scope"])
    assert row.payload is not None
    assert row.version >= 1
    assert store.load(OVERLAY["scope"] + ":answer").payload is not None
    assert len(h.starter.starts) == 7
    assert answer.status is EnumFloorFillStatus.OK


def test_absent_row_vs_unreadable_store(tmp_path: Path) -> None:
    absent = harness(store=_sqlite(tmp_path), lines=ONE_TASK)
    assert absent.orchestrator().run(TICK).status is EnumFloorFillStatus.OK
    assert len(absent.starter.starts) == 1

    class Unreachable:
        def load(self, key: str) -> ModelStateRead:
            raise FloorFillUnreadableError("store-unreachable:down")

        def compare_and_set(self, *args: object) -> bool:
            raise AssertionError("nothing is written to a store that cannot be read")

    down = harness(store=Unreachable(), lines=ONE_TASK)
    answer = down.orchestrator().run(TICK)
    assert answer.lines == ("UNKNOWN fill-state:store-unreachable:down",)
    assert answer.status is EnumFloorFillStatus.FAILED
    assert answer.exit_code != 0
    assert down.starter.starts == []


# --------------------------------------------------------------------------- fail closed (AC4)


def test_state_unreadable_fails_closed() -> None:
    store = InMemoryFloorStateStore()
    store.compare_and_set(OVERLAY["scope"], 0, 1, "{not json")
    counting = CountingStore(store)
    h = harness(store=counting, lines=ONE_TASK)
    answer = h.orchestrator().run(TICK)
    assert answer.lines == ("UNKNOWN fill-state:unparseable",)
    assert answer.status is EnumFloorFillStatus.FAILED
    assert answer.exit_code == 4
    assert h.starter.starts == []
    assert counting.writes == []
    assert h.tickets.calls == []


def test_missing_overlay_key_refused_naming_key() -> None:
    broken = {k: v for k, v in OVERLAY.items() if k != "plans_root"}
    with pytest.raises(FloorFillConfigError) as error:
        load_floor_fill_config(broken)
    assert error.value.keys == ("floor_fill.plans_root",)


def test_stale_seq_refused() -> None:
    inner = InMemoryFloorStateStore()

    class Interfering:
        """Another writer lands between this command's read and its first write."""

        def __init__(self) -> None:
            self.done = False

        def load(self, key: str) -> ModelStateRead:
            return inner.load(key)

        def compare_and_set(
            self, key: str, expected: int, new: int, payload: str
        ) -> bool:
            if not self.done:
                self.done = True
                inner.compare_and_set(key, expected, expected + 1, "{}")
            return inner.compare_and_set(key, expected, new, payload)

    h = harness(store=Interfering(), lines=ONE_TASK)
    answer = h.orchestrator().run(TICK)
    assert answer.lines == ("STALE-STATE",)
    assert answer.status is EnumFloorFillStatus.FAILED
    assert answer.exit_code != 0
    assert h.starter.starts == []


@pytest.mark.parametrize(
    ("source", "scripted", "line"),
    [
        ("floor", {"facts": ScriptedFacts(None)}, "UNKNOWN floor-unread:no-answer"),
        (
            "pool",
            {"pool": ScriptedPool(5, unreadable=True)},
            "UNKNOWN pool-unread:no host answered",
        ),
        (
            "brake",
            {
                "facts": ScriptedFacts(
                    floor_record(ONE_TASK, generated_at=T0),
                    unreadable=frozenset({"brake"}),
                )
            },
            "UNKNOWN brake-unread:no-reading",
        ),
        (
            "stop-signal",
            {
                "facts": ScriptedFacts(
                    floor_record(ONE_TASK, generated_at=T0),
                    unreadable=frozenset({"stop-signal"}),
                )
            },
            "UNKNOWN stop-signal-unread:no-reading",
        ),
    ],
)
def test_floor_unread_is_unknown(
    source: str, scripted: dict[str, Any], line: str
) -> None:
    ledger = ScriptedLedger()
    h = harness(lines=ONE_TASK, ledger=ledger, **scripted)
    # An earlier pass reserved the key and its lane's CLAIM has been appended since: S2 still resolves it.
    seeded = harness(lines=ONE_TASK, store=h.store, ledger=ledger)
    seeded.orchestrator().run(TICK)
    (start,) = seeded.starter.starts
    ledger.claim(start.lane, start.key)
    answer = h.orchestrator().run(TICK)
    assert answer.lines == (line,)
    assert h.starter.starts == []
    assert answer.status is EnumFloorFillStatus.DEGRADED
    assert answer.exit_code != 0
    from omnimarket.models.floor_fill import ModelFloorFillState

    stored = h.store.load(OVERLAY["scope"]).payload
    assert stored is not None
    assert (
        ModelFloorFillState.model_validate_json(stored).entries[start.key].state.value
        == "SENT"
    )


def test_unreadable_ledger_resolves_nothing() -> None:
    ledger = ScriptedLedger()
    first = harness(lines=ONE_TASK, ledger=ledger)
    first.orchestrator().run(TICK)
    before = first.store.load(OVERLAY["scope"])
    ledger.unreadable = True
    counting = CountingStore(first.store)
    again = harness(lines=ONE_TASK, store=counting, ledger=ledger)
    answer = again.orchestrator().run(TICK)
    assert answer.lines == ("UNKNOWN ledger-unread:ledger-unreadable",)
    # The state row is untouched; only the stored answer (which names the unread source) is written.
    assert [w for w in counting.writes if not w.endswith(":answer")] == []
    assert again.starter.starts == []
    assert first.store.load(OVERLAY["scope"]) == before
    assert answer.status is EnumFloorFillStatus.DEGRADED
    assert answer.exit_code == 3


def test_degraded_status_nonzero() -> None:
    h = harness(pool=ScriptedPool(3, unreadable=True))
    answer = h.orchestrator().run(TICK)
    assert answer.status is EnumFloorFillStatus.DEGRADED
    assert answer.exit_code == 3
    assert answer.lines == ("UNKNOWN pool-unread:no host answered",)


def test_timeout_order_refused() -> None:
    with pytest.raises(FloorFillConfigError) as error:
        load_floor_fill_config(overlay(command_timeout_seconds=600))
    assert set(error.value.keys) == {
        "floor_fill.command_timeout_seconds",
        "floor_fill.reserved_timeout_seconds",
    }


# --------------------------------------------------------------------------- retry after a crash (AC5)


def _per_attempt(starts: list[ModelLaneStart]) -> list[tuple[str, int]]:
    return [(s.key, s.attempt) for s in starts]


@pytest.mark.parametrize("kill_after_write", [1, 2])
def test_crash_after_each_write(tmp_path: Path, kill_after_write: int) -> None:
    """Killed after the ticket was stored (1) and after the reserve write (2), then a second pass."""
    real = _sqlite(tmp_path)
    ledger = ScriptedLedger()
    first = harness(
        lines=ONE_TASK, store=CrashAfterWrites(real, kill_after_write), ledger=ledger
    )
    with pytest.raises(SimulatedCrash):
        first.orchestrator().run(TICK)
    second = harness(lines=ONE_TASK, store=real, ledger=ledger)
    second.starter = first.starter
    second.tickets = first.tickets
    second.orchestrator().run(TICK)
    if kill_after_write == 1:
        # The ticket was stored; the next pass reserves and starts attempt 1 exactly once.
        assert _per_attempt(second.starter.starts) == [("floor:alpha:a1", 1)]
        return
    # Reserved, never started: left alone before the timeout...
    assert second.starter.starts == []
    second.clock.advance(seconds=601)
    second.orchestrator().run(TICK)
    # ...and after it, cleared and offered once more as attempt 2.
    assert _per_attempt(second.starter.starts) == [("floor:alpha:a1", 2)]


def test_crash_after_start_returned_before_confirmation(tmp_path: Path) -> None:
    real = _sqlite(tmp_path)
    ledger = ScriptedLedger()

    def crash(start: ModelLaneStart) -> None:
        raise SimulatedCrash

    first = harness(
        lines=ONE_TASK,
        store=real,
        ledger=ledger,
        starter=ScriptedStarter(after_start=crash),
    )
    with pytest.raises(SimulatedCrash):
        first.orchestrator().run(TICK)
    (started,) = first.starter.starts
    # The lane's CLAIM was appended before the kill: the second pass confirms it SENT and starts nothing.
    ledger.claim(started.lane, started.key)
    second = harness(
        lines=ONE_TASK,
        store=real,
        ledger=ledger,
        starter=first.starter,
        tickets=first.tickets,
    )
    second.orchestrator().run(TICK)
    assert _per_attempt(first.starter.starts) == [("floor:alpha:a1", 1)]
    # With no CLAIM it would be left alone before the timeout and offered again, once, after it.
    real2 = SqliteFloorStateStore(str(tmp_path / "lone.db"), busy_timeout_seconds=5)
    lone = harness(
        lines=ONE_TASK, store=real2, starter=ScriptedStarter(after_start=crash)
    )
    with pytest.raises(SimulatedCrash):
        lone.orchestrator().run(TICK)
    lone.starter.after_start = None  # the process is back; the runner starts normally
    retry = harness(
        lines=ONE_TASK, store=real2, starter=lone.starter, tickets=lone.tickets
    )
    retry.orchestrator().run(TICK)
    assert len(lone.starter.starts) == 1
    retry.clock.advance(seconds=601)
    retry.orchestrator().run(TICK)
    assert _per_attempt(lone.starter.starts) == [
        ("floor:alpha:a1", 1),
        ("floor:alpha:a1", 2),
    ]


# --------------------------------------------------------------------------- one lane per key (AC9)


class LateCreateTickets(ScriptedTickets):
    """A create that completes after the deadline and a search that lags it: two tickets exist for the key."""

    def __init__(self) -> None:
        super().__init__()
        self.issues: list[str] = []
        self.first = True

    def mint(self, key: str) -> ModelMintResult:
        self.calls.append(f"mint:{key}")
        if self.first:
            self.first = False
            self.issues.append("T-LATE")  # the create completed, but past the deadline
            return ModelMintResult(state="unavailable", cause="deadline")
        if len(self.issues) == 1:
            self.issues.append(
                "T-SECOND"
            )  # the search lagged: it did not return T-LATE
            return ModelMintResult(state="ticket", ticket="T-SECOND")
        return ModelMintResult(state="ticket", ticket=self.issues[0])


def test_duplicate_ticket_one_lane() -> None:
    ledger = ScriptedLedger()
    tickets = LateCreateTickets()
    h = harness(
        lines=ONE_TASK,
        tickets=tickets,
        ledger=ledger,
        starter=ScriptedStarter(after_start=lambda s: ledger.claim(s.lane, s.key)),
    )
    first = h.orchestrator().run(TICK)
    assert h.starter.starts == []
    assert tickets.issues == ["T-LATE"]
    assert any("cause=mint-refused:deadline" in line for line in first.lines)
    h.orchestrator().run(TICK)
    h.clock.advance(minutes=1)
    h.orchestrator().run(TICK)
    assert len(tickets.issues) == 2  # two tickets exist for the key
    assert len(h.starter.starts) == 1  # and exactly one lane was started for it
    assert len(ledger.claims) == 1
    assert LINES4  # the four-line fixture stays importable for the parity scenario
