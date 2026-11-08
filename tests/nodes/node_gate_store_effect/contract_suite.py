# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate store's contract suite (rebuild plan section 7.5, "the bar that counts").

Nine racing reservations, a handover, a kill at each step, a duplicate delivery and keys
repeated by a replay, plus the rules of the store: a consumed reservation counts until its
process is read as gone, a safety write commits before it counts, and the order advances one
position at a time by the holder at the current epoch.

Frozen with a hash (``contract_suite.sha256``, checked by ``test_contract_suite_frozen``): any
backend (the in-memory twin, later Postgres) must pass these checks unchanged through a factory
``(policy, fault_hook) -> ProtocolGateStore``. A backend that cannot raise at each stage the
hook names cannot pass. Every check is a plain function that raises ``AssertionError``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStorePolicy,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore

Factory = Callable[
    [ModelGateStorePolicy, Callable[[str], None] | None], ProtocolGateStore
]
OP = EnumGateStoreOperation
OUT = EnumGateStoreOutcome

#: Stages a kill lands on; a backend raises the hook's error at each, in this order.
KILL_STAGES = ("before-apply", "after-apply", "before-commit")

#: An invented policy: three pieces, a lifetime of 60 seconds and a three-step order.
POLICY = ModelGateStorePolicy(
    backend=EnumGateStoreBackend.IN_MEMORY,
    limits={"alpha": 3, "beta": 2, "gamma": 1},
    reservation_ttl_seconds=60,
    order=("alpha", "beta", "gamma"),
)


class SuiteKillError(Exception):
    """The error a kill injects at a stage."""


def req(op: OP, key: str | None, **fields: object) -> ModelGateStoreRequest:
    """A request with a correlation id derived from its step key."""
    return ModelGateStoreRequest.model_validate(
        {"correlation_id": f"c-{key}", "operation": op, "step_key": key, **fields}
    )


def reserve(
    store: ProtocolGateStore, piece: str, rid: str, now: float = 0.0
) -> ModelGateStoreResult:
    return store.transact(
        req(OP.RESERVE, f"run/{rid}", piece=piece, reservation_id=rid, now=now)
    )


def read(store: ProtocolGateStore, piece: str) -> ModelGateStoreResult:
    return store.transact(req(OP.READ, None, piece=piece))


def _race(
    calls: list[Callable[[], ModelGateStoreResult]],
) -> list[ModelGateStoreResult]:
    barrier = threading.Barrier(len(calls))
    results: list[ModelGateStoreResult | None] = [None] * len(calls)

    def run(i: int) -> None:
        barrier.wait()
        results[i] = calls[i]()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r is not None for r in results), "a racing call returned nothing"
    return [r for r in results if r is not None]


def check_nine_racing_reservations(factory: Factory) -> None:
    """Nine reservations race for a piece with limit 3: exactly 3 hold, 6 are refused."""
    store = factory(POLICY, None)
    results = _race([lambda i=i: reserve(store, "alpha", f"r{i}") for i in range(9)])
    admitted = [r for r in results if r.outcome is OUT.OK]
    refused = [r for r in results if r.outcome is OUT.REFUSED_LIMIT]
    assert len(admitted) == 3, f"{len(admitted)} admitted over a limit of 3"
    assert len(refused) == 6, f"{len(refused)} refused, expected 6"
    assert read(store, "alpha").counted == 3, "the store counts other than 3"


def check_consumed_counts_until_process_gone(factory: Factory) -> None:
    """A consumed reservation is not freed by a release that reads no process gone."""
    store = factory(POLICY, None)
    for i in range(3):
        assert reserve(store, "alpha", f"r{i}").outcome is OUT.OK
    consumed = store.transact(
        req(OP.CONSUME, "run/consume-r0", reservation_id="r0", now=1.0)
    )
    assert consumed.outcome is OUT.OK
    again = store.transact(
        req(OP.CONSUME, "run/consume-r0-again", reservation_id="r0", now=1.0)
    )
    assert again.outcome is OUT.REFUSED_CONSUMED, "a reservation is single use"
    live = store.transact(
        req(OP.RELEASE, "run/release-r0", reservation_id="r0", process_gone=False)
    )
    assert live.outcome is OUT.REFUSED_PROCESS_LIVE
    assert reserve(store, "alpha", "r3").outcome is OUT.REFUSED_LIMIT
    gone = store.transact(
        req(OP.RELEASE, "run/release-r0-gone", reservation_id="r0", process_gone=True)
    )
    assert gone.outcome is OUT.OK
    assert reserve(store, "alpha", "r4").outcome is OUT.OK


def check_safety_write_commits_before_it_counts(factory: Factory) -> None:
    """After a stop on a piece returns, no reservation on that piece is admitted."""
    store = factory(POLICY, None)
    assert reserve(store, "alpha", "early").outcome is OUT.OK
    stop = store.transact(
        req(OP.RECORD_SAFETY, "run/stop-alpha", piece="alpha", safety_state="stop")
    )
    assert stop.outcome is OUT.OK
    assert stop.epoch > 0
    results = _race([lambda i=i: reserve(store, "alpha", f"s{i}") for i in range(9)])
    assert all(r.outcome is OUT.REFUSED_STOP for r in results), "a start passed a stop"
    consume = store.transact(
        req(OP.CONSUME, "run/consume-early", reservation_id="early", now=1.0)
    )
    assert consume.outcome is OUT.REFUSED_STOP
    assert reserve(store, "beta", "b0").outcome is OUT.OK, (
        "a stop holds its own piece only"
    )
    clear = store.transact(
        req(OP.RECORD_SAFETY, "run/clear-alpha", piece="alpha", safety_state="clear")
    )
    assert clear.outcome is OUT.OK
    assert reserve(store, "alpha", "after-clear").outcome is OUT.OK


def check_handover_is_fenced(factory: Factory) -> None:
    """Two handovers at the same epoch: one wins, the other is fenced out."""
    store = factory(POLICY, None)
    results = _race(
        [
            lambda w=w: store.transact(
                req(OP.HANDOVER, f"run/handover-{w}", holder=w, expected_epoch=0)
            )
            for w in ("writer-a", "writer-b")
        ]
    )
    outcomes = sorted(r.outcome for r in results)
    assert outcomes == [OUT.OK, OUT.REFUSED_FENCE], f"handover outcomes {outcomes}"
    won = next(r for r in results if r.outcome is OUT.OK)
    stale = store.transact(
        req(OP.HANDOVER, "run/handover-stale", holder="writer-c", expected_epoch=0)
    )
    assert stale.outcome is OUT.REFUSED_FENCE
    assert read(store, "alpha").holder == won.holder, (
        "a fenced handover moved the holder"
    )


def check_kill_at_each_stage(factory: Factory) -> None:
    """A kill at each stage leaves the store as it was; the replay then commits once."""
    for stage in KILL_STAGES:
        armed = {"on": True}

        def hook(at: str, stage: str = stage, armed: dict[str, bool] = armed) -> None:
            if armed["on"] and at == stage:
                raise SuiteKillError(stage)

        store = factory(POLICY, hook)
        try:
            reserve(store, "alpha", "k0")
        except SuiteKillError:
            pass
        else:
            raise AssertionError(f"the kill at {stage} did not reach the caller")
        armed["on"] = False
        before = read(store, "alpha")
        assert before.epoch == 0, f"{stage}: a partial write"
        assert before.counted == 0, f"{stage}: a partial write"
        first = reserve(store, "alpha", "k0")
        assert first.outcome is OUT.OK, f"{stage}: the replay was not applied"
        second = reserve(store, "alpha", "k0")
        assert second.outcome is OUT.DUPLICATE, f"{stage}: applied twice"
        assert read(store, "alpha").counted == 1


def check_duplicate_delivery_and_replayed_keys(factory: Factory) -> None:
    """The same step key twice writes once and returns the first answer's epoch."""
    store = factory(POLICY, None)
    first = reserve(store, "alpha", "d0")
    second = reserve(store, "alpha", "d0")
    assert first.outcome is OUT.OK
    assert second.outcome is OUT.DUPLICATE
    assert second.epoch == first.epoch, "a duplicate advanced the epoch"
    replay = store.transact(
        req(OP.RESERVE, "run/d0", piece="alpha", reservation_id="d1", now=0.0)
    )
    assert replay.outcome is OUT.DUPLICATE, "a replayed key with new content wrote"
    assert read(store, "alpha").counted == 1
    refused = reserve(store, "gamma", "g0")
    assert refused.outcome is OUT.OK
    over = reserve(store, "gamma", "g1")
    assert over.outcome is OUT.REFUSED_LIMIT
    assert reserve(store, "gamma", "g1").outcome is OUT.DUPLICATE, (
        "a replayed refusal returns the first refusal and never retries"
    )


def check_order_writer_advances_one_position(factory: Factory) -> None:
    """The holder at the current epoch advances the order one position at a time."""
    store = factory(POLICY, None)
    held = store.transact(
        req(OP.HANDOVER, "run/hold", holder="writer-a", expected_epoch=0)
    )
    epoch = held.epoch
    skip = store.transact(
        req(
            OP.ADVANCE_ORDER,
            "run/skip",
            holder="writer-a",
            expected_epoch=epoch,
            order_position=2,
        )
    )
    assert skip.outcome is OUT.REFUSED_ORDER
    stranger = store.transact(
        req(
            OP.ADVANCE_ORDER,
            "run/stranger",
            holder="writer-b",
            expected_epoch=epoch,
            order_position=1,
        )
    )
    assert stranger.outcome is OUT.REFUSED_FENCE
    for position in (1, 2, 3):
        step = store.transact(
            req(
                OP.ADVANCE_ORDER,
                f"run/advance-{position}",
                holder="writer-a",
                expected_epoch=epoch,
                order_position=position,
            )
        )
        assert step.outcome is OUT.OK
        assert step.order_position == position
        epoch = step.epoch
    past = store.transact(
        req(
            OP.ADVANCE_ORDER,
            "run/past",
            holder="writer-a",
            expected_epoch=epoch,
            order_position=4,
        )
    )
    assert past.outcome is OUT.REFUSED_ORDER, "the order advanced past its length"
    stale = store.transact(
        req(
            OP.ADVANCE_ORDER,
            "run/stale",
            holder="writer-a",
            expected_epoch=epoch - 1,
            order_position=4,
        )
    )
    assert stale.outcome is OUT.REFUSED_FENCE


def check_refusals_are_typed(factory: Factory) -> None:
    """An unknown piece, a missing step key and a lapsed reservation are refused by type."""
    store = factory(POLICY, None)
    assert reserve(store, "nowhere", "u0").outcome is OUT.REFUSED_UNKNOWN_PIECE
    keyless = store.transact(
        req(OP.RESERVE, None, piece="alpha", reservation_id="u1", now=0.0)
    )
    assert keyless.outcome is OUT.REFUSED_INVALID
    assert reserve(store, "gamma", "e0", now=0.0).outcome is OUT.OK
    lapsed = store.transact(
        req(OP.CONSUME, "run/consume-e0", reservation_id="e0", now=61.0)
    )
    assert lapsed.outcome is OUT.REFUSED_EXPIRED
    assert reserve(store, "gamma", "e1", now=61.0).outcome is OUT.OK, (
        "a lapsed reservation kept counting"
    )


CHECKS: tuple[Callable[[Factory], None], ...] = (
    check_nine_racing_reservations,
    check_consumed_counts_until_process_gone,
    check_safety_write_commits_before_it_counts,
    check_handover_is_fenced,
    check_kill_at_each_stage,
    check_duplicate_delivery_and_replayed_keys,
    check_order_writer_advances_one_position,
    check_refusals_are_typed,
)
