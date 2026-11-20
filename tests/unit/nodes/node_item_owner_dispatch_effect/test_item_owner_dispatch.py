# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Item owner dispatch (OMN-25981): the fold, one owner per item, the rebinding and the unowned reading."""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.inmemory_adapters import (
    InMemoryBriefPreparer,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelPlaceOutcome,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.adapters.inmemory_adapters import (
    InMemoryLedger,
    InMemoryPlanOwner,
    InMemoryReservation,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.handlers import (
    handler_item_owner_dispatch,
    handler_unowned_reading,
    item_fold,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.handlers.handler_item_owner_dispatch import (
    HandlerItemOwnerDispatch,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.handlers.handler_unowned_reading import (
    EXIT_CODES,
    HandlerUnownedReading,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.handlers.item_fold import (
    fold_items,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.models import (
    EnumItemSource,
    EnumItemState,
    EnumOwnerSkip,
    EnumReadingVerdict,
    ModelItemOwnerRequest,
    ModelItemOwnerResult,
    ModelOwnerPlaced,
    ModelUnownedReading,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.overlay import (
    ModelItemOwnerOverlay,
)

pytestmark = pytest.mark.unit

NODE = Path(item_fold.__file__).resolve().parents[1]
OVERLAY = ModelItemOwnerOverlay.model_validate(
    yaml.safe_load((NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
)
POLICY = OVERLAY.policy
T0 = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)

BLOCKED = (
    "2026-11-19T11:00:00Z | TERMINAL | lane=fix-a-1000 | ticket=OMN-500 | pr=omnimarket#1 | "
    "outcome=blocked | friction=none | blocked on the broker login"
)
RULING = (
    "2026-11-19T11:01:00Z | RULING | lane=orchestrator-x | ticket=OMN-501 | kind=process | "
    'mechanism-pending=2026-11-19T11:01:00Z | "Make it mechanical."'
)
CAPTURE = (
    "2026-11-19T11:02:00Z | STATUS | lane=operator-capture | kind=ask | item=cap-0123456789 | "
    'ask=ask-0123456789 | state=open | actor=script:handler-capture-process | capture=abc | "Do the thing."'
)
PLAN_RULE = "ledger-cutover-owner"


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class ClaimingPlacer:
    """The remote-lane runner's visible effect: one CLAIM per started lane, carrying the item's cause."""

    def __init__(self, ledger: InMemoryLedger, clock: Clock) -> None:
        self.ledger = ledger
        self.clock = clock
        self.placed: list[tuple[str, ...]] = []
        self.lock = threading.Lock()

    def place(self, argv: tuple[str, ...]) -> ModelPlaceOutcome:
        lane = argv[argv.index("--lane") + 1]
        cause = argv[argv.index("--cause") + 1]
        with self.lock:
            self.placed.append(argv)
        self.ledger.append(
            f"{_stamp(self.clock())} | CLAIM | lane={lane} | ticket=OMN-1 | actor=script:onex-remote-lane | "
            f"model=sonnet | cause={cause} | remote lane"
        )
        return ModelPlaceOutcome(started=True, detail="placed")


def _handler(
    ledger: InMemoryLedger,
    clock: Clock,
    placer: ClaimingPlacer,
    *,
    reservation: InMemoryReservation | None = None,
    plan: InMemoryPlanOwner | None = None,
) -> HandlerItemOwnerDispatch:
    return HandlerItemOwnerDispatch(
        overlay=OVERLAY,
        rows=ledger,
        plan_owner=plan or InMemoryPlanOwner(),
        reservation=reservation or InMemoryReservation(),
        briefs=InMemoryBriefPreparer(),
        runner=placer,
        appender=ledger,
        runner_path="/runner/onex_remote_lane.py",
        claude_dir="",
        clock=clock,
    )


def _owned_rows(ledger: InMemoryLedger) -> list[str]:
    return [
        row
        for row in ledger.rows
        if f"lane={POLICY.node_lane}" in row and "state=owned" in row
    ]


# --- criterion 1: one item per source event, deterministic id, replay adds none ---------------------------------


def test_four_sources_fold_to_four_items_and_a_replay_adds_none() -> None:
    rows = (BLOCKED, RULING, CAPTURE)
    first = fold_items(rows, (PLAN_RULE,), _stamp(T0), POLICY)
    assert sorted(item.source for item in first.items) == sorted(EnumItemSource)
    assert all(item.state is EnumItemState.OPEN for item in first.items)
    assert {
        item.item_id
        for item in first.items
        if item.source is EnumItemSource.OPERATOR_CAPTURE
    } == {"cap-0123456789"}
    replay = fold_items(
        rows + rows, (PLAN_RULE, PLAN_RULE), _stamp(T0 + timedelta(minutes=5)), POLICY
    )
    assert {item.item_id for item in replay.items} == {
        item.item_id for item in first.items
    }
    assert len(replay.items) == 4


def test_a_successor_claim_a_mechanism_correction_and_a_closed_ask_end_their_items() -> (
    None
):
    rows = (
        BLOCKED,
        RULING,
        CAPTURE,
        "2026-11-19T11:30:00Z | CLAIM | lane=fix-a-1130 | ticket=OMN-500 | actor=codex | model=x | next lane",
        "2026-11-19T11:31:00Z | CORRECTION | lane=orchestrator-x | corrects=2026-11-19T11:01:00Z | "
        "mechanism=omnimarket/src/x.py | wired",
        "2026-11-19T11:32:00Z | STATUS | lane=operator-capture | item=cap-0123456789 | state=done | done",
    )
    items = fold_items(rows, (), _stamp(T0), POLICY).items
    assert len(items) == 3
    assert all(item.state is EnumItemState.DONE for item in items)


# --- criterion 2: exactly one owner lane, owner written onto the item -----------------------------------------


def test_one_owner_per_item_over_two_ticks_places_one_lane_and_one_owned_row() -> None:
    ledger = InMemoryLedger((BLOCKED,))
    clock = Clock(T0)
    placer = ClaimingPlacer(ledger, clock)
    first = _handler(ledger, clock, placer).handle(
        ModelItemOwnerRequest(run_id="wf-1/run-a")
    )
    assert [p.started for p in first.placed] == [True]
    clock.at = T0 + timedelta(minutes=15)
    second = _handler(ledger, clock, placer).handle(
        ModelItemOwnerRequest(run_id="wf-2/run-b")
    )
    rerun = _handler(ledger, clock, placer).handle(
        ModelItemOwnerRequest(run_id="wf-2/run-b")
    )
    assert second.placed == ()
    assert rerun.placed == ()
    assert len(placer.placed) == 1
    owned = _owned_rows(ledger)
    assert len(owned) == 1
    item = first.items[0]
    lane = first.placed[0].lane
    assert f"item={item.item_id}" in owned[0]
    assert f"owner={lane}" in owned[0]
    assert f"rev={item.revision}" in owned[0]
    assert "run=wf-1/run-a" in owned[0]
    assert "gen=1" in owned[0]
    after = fold_items(tuple(ledger.rows), (), _stamp(clock.at), POLICY).items[0]
    assert after.state is EnumItemState.OWNED
    assert after.running_lane == lane == after.accountable_owner


def test_one_owner_per_item_across_two_concurrent_runs() -> None:
    ledger = InMemoryLedger((BLOCKED,))
    clock = Clock(T0)
    placer = ClaimingPlacer(ledger, clock)
    reservation = InMemoryReservation()
    gate = threading.Barrier(2)

    class RacingRows:
        """Both runs read the rows before either places: the race the reservation must decide."""

        def __init__(self) -> None:
            self.calls = 0

        def read(self) -> tuple[str, ...]:
            self.calls += 1
            rows = ledger.read()
            if self.calls == 1:
                gate.wait(timeout=10)
            return rows

    results: list[ModelItemOwnerResult] = []

    def run(name: str) -> None:
        handler = HandlerItemOwnerDispatch(
            overlay=OVERLAY,
            rows=RacingRows(),
            plan_owner=InMemoryPlanOwner(),
            reservation=reservation,
            briefs=InMemoryBriefPreparer(),
            runner=placer,
            appender=ledger,
            runner_path="/runner/onex_remote_lane.py",
            claude_dir="",
            clock=clock,
        )
        results.append(handler.handle(ModelItemOwnerRequest(run_id=name)))

    threads = [threading.Thread(target=run, args=(f"run-{n}",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == 2
    assert len(placer.placed) == 1
    assert len(_owned_rows(ledger)) == 1
    assert sorted(len(r.placed) for r in results) == [0, 1]
    assert any(
        (item, EnumOwnerSkip.RESERVED) in r.skipped
        for r in results
        for item in [r.items[0].item_id]
    )


# --- criterion 3: a live CLAIM or an item younger than the bound gets no lane ---------------------------------


def test_an_item_whose_owner_holds_a_live_claim_gets_no_lane() -> None:
    item_id = fold_items((BLOCKED,), (), _stamp(T0), POLICY).items[0].item_id
    ledger = InMemoryLedger(
        (
            BLOCKED,
            f"2026-11-19T11:40:00Z | CLAIM | lane=someone-1140 | ticket=OMN-9 | actor=x | model=x | "
            f"cause={POLICY.cause_prefix}:{item_id} | took it by hand",
        )
    )
    clock = Clock(T0)
    placer = ClaimingPlacer(ledger, clock)
    result = _handler(ledger, clock, placer).handle(ModelItemOwnerRequest(run_id="r"))
    assert result.placed == ()
    assert placer.placed == []
    assert result.skipped == ((item_id, EnumOwnerSkip.RUNNING),)
    assert result.items[0].running_lane == "someone-1140"


def test_an_item_at_29_minutes_gets_no_lane() -> None:
    ledger = InMemoryLedger((BLOCKED,))
    clock = Clock(datetime(2026, 11, 19, 11, 29, 0, tzinfo=UTC))
    placer = ClaimingPlacer(ledger, clock)
    result = _handler(ledger, clock, placer).handle(ModelItemOwnerRequest(run_id="r"))
    assert result.placed == ()
    assert placer.placed == []
    assert [skip for _, skip in result.skipped] == [EnumOwnerSkip.YOUNG]


# --- criterion 4: a blocked TERMINAL of the owner lane keeps the accountable owner and rebinds once -------------


def test_a_blocked_owner_lane_leaves_the_accountable_owner_and_the_next_tick_past_the_bound_rebinds() -> (
    None
):
    ledger = InMemoryLedger((BLOCKED,))
    clock = Clock(T0)
    placer = ClaimingPlacer(ledger, clock)
    first = _handler(ledger, clock, placer).handle(ModelItemOwnerRequest(run_id="r1"))
    lane = first.placed[0].lane
    ended = T0 + timedelta(minutes=10)
    ledger.append(
        f"{_stamp(ended)} | TERMINAL | lane={lane} | ticket=OMN-1 | outcome=blocked | friction=none | stuck"
    )
    clock.at = ended + timedelta(minutes=1)
    item = fold_items(tuple(ledger.rows), (), _stamp(clock.at), POLICY).items
    assert len(item) == 1, "the owner lane's own blocked TERMINAL is not a second item"
    assert item[0].accountable_owner == lane
    assert item[0].running_lane is None
    assert item[0].state is EnumItemState.OPEN
    assert item[0].unowned_since == _stamp(ended)
    young = _handler(ledger, clock, placer).handle(ModelItemOwnerRequest(run_id="r2"))
    assert young.placed == ()
    clock.at = ended + timedelta(minutes=31)
    later = _handler(ledger, clock, placer).handle(ModelItemOwnerRequest(run_id="r3"))
    again = _handler(ledger, clock, placer).handle(ModelItemOwnerRequest(run_id="r3"))
    assert [p.lane for p in later.placed] == [lane.replace("-g1", "-g2")]
    assert again.placed == ()
    assert len(placer.placed) == 2
    assert len(_owned_rows(ledger)) == 2


def test_a_placement_that_failed_appends_no_owner_and_is_retried() -> None:
    ledger = InMemoryLedger((BLOCKED,))
    clock = Clock(T0)

    class Failing:
        def place(self, argv: tuple[str, ...]) -> ModelPlaceOutcome:
            return ModelPlaceOutcome(started=False, detail="no host")

    handler = HandlerItemOwnerDispatch(
        overlay=OVERLAY,
        rows=ledger,
        plan_owner=InMemoryPlanOwner(),
        reservation=InMemoryReservation(),
        briefs=InMemoryBriefPreparer(),
        runner=Failing(),
        appender=ledger,
        runner_path="/r",
        claude_dir="",
        clock=clock,
    )
    result = handler.handle(ModelItemOwnerRequest(run_id="r"))
    assert [p.skip for p in result.placed] == [EnumOwnerSkip.PLACEMENT_FAILED]
    assert _owned_rows(ledger) == []
    placer = ClaimingPlacer(ledger, clock)
    retried = _handler(ledger, clock, placer).handle(ModelItemOwnerRequest(run_id="r2"))
    assert [p.started for p in retried.placed] == [True]


# --- criterion 5 and AC2: the reading -------------------------------------------------------------------------


def _reading(
    ledger: InMemoryLedger, at: datetime, plan: InMemoryPlanOwner | None = None
) -> ModelUnownedReading:
    return HandlerUnownedReading(
        overlay=OVERLAY,
        rows=ledger,
        plan_owner=plan or InMemoryPlanOwner(),
        appender=ledger,
        clock=Clock(at),
    ).read()


def test_unowned_reading_is_finding_past_the_bound_and_fails() -> None:
    ledger = InMemoryLedger((BLOCKED,))
    reading = _reading(ledger, T0)
    assert reading.verdict is EnumReadingVerdict.FINDING
    assert EXIT_CODES[reading.verdict] != 0
    assert len(reading.unowned) == 1
    row = ledger.rows[-1]
    assert row == reading.status_row
    assert f"lane={POLICY.node_lane}" in row
    assert "verdict=FINDING" in row
    assert " | STATUS | " in row


def test_unowned_reading_is_unread_when_the_facts_cannot_be_read() -> None:
    ledger = InMemoryLedger((BLOCKED,))
    ledger.unreadable = "bus down"
    reading = _reading(ledger, T0)
    assert reading.verdict is EnumReadingVerdict.UNREAD
    assert EXIT_CODES[reading.verdict] != 0
    plan = InMemoryPlanOwner()
    plan.unreadable = "stale answer"
    assert (
        _reading(InMemoryLedger((BLOCKED,)), T0, plan).verdict
        is EnumReadingVerdict.UNREAD
    )


def test_unowned_reading_is_ok_for_a_young_or_owned_item() -> None:
    young = _reading(
        InMemoryLedger((BLOCKED,)), datetime(2026, 11, 19, 11, 29, 0, tzinfo=UTC)
    )
    assert young.verdict is EnumReadingVerdict.OK
    assert EXIT_CODES[young.verdict] == 0
    ledger = InMemoryLedger((BLOCKED,))
    clock = Clock(T0)
    _handler(ledger, clock, ClaimingPlacer(ledger, clock)).handle(
        ModelItemOwnerRequest(run_id="r")
    )
    owned = _reading(ledger, T0 + timedelta(minutes=45))
    assert owned.verdict is EnumReadingVerdict.OK
    assert owned.unowned == ()


# --- AC3: the owner binding never admits or refuses work ------------------------------------------------------

_VERDICT_WORDS = re.compile(r"admit|admission|refus|reject|deny|denied", re.IGNORECASE)


def test_never_admits_or_refuses_any_lane_ruling_or_plan_step() -> None:
    for enum in (EnumOwnerSkip, EnumReadingVerdict, EnumItemState):
        assert not [member for member in enum if _VERDICT_WORDS.search(member.value)]
    for model in (ModelItemOwnerResult, ModelOwnerPlaced, ModelUnownedReading):
        assert not [name for name in model.model_fields if _VERDICT_WORDS.search(name)]
    for module in (
        item_fold,
        handler_item_owner_dispatch,
        handler_unowned_reading,
    ):
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        assert not _VERDICT_WORDS.search(source), module.__name__
    ledger = InMemoryLedger((BLOCKED, RULING, CAPTURE))
    clock = Clock(T0 + timedelta(hours=2))
    placer = ClaimingPlacer(ledger, clock)
    reservation = InMemoryReservation()
    plan = InMemoryPlanOwner((PLAN_RULE,))
    handler = _handler(ledger, clock, placer, reservation=reservation, plan=plan)
    result = handler.handle(ModelItemOwnerRequest(run_id="r"))
    assert len(result.items) == 4, "every source event is an item, whatever it says"
    assert {item.item_id for item in result.items} == {
        p.item_id for p in result.placed
    } | {item for item, _ in result.skipped}


def test_contract_terminal_event_is_the_dispatch_completed_topic_the_broker_acl_may_write() -> (
    None
):
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    completed = "onex.evt.omnimarket.item-owner-dispatch-completed.v1"
    assert contract["terminal_event"] == completed
    assert contract["topics"]["dispatch_completed"] == completed
    assert completed in contract["externally_consumed_topics"]
    writes = {
        grant["name"]
        for acl in contract["broker_acl_grants"]
        for grant in acl["grants"]
        if grant["operation"] == "WRITE"
    }
    assert completed in writes
