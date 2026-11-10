# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance tests of the order writer (OMN-22938): the lifecycle and restart, the fence and placement, the overlay."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    ModelGateStoreRequest,
)
from omnimarket.models.model_order_advance import (
    EnumOrderAdvanceOutcome,
    EnumPieceRecordOperation,
    EnumPieceState,
    EnumRunEngineBackend,
    ModelOrderAdvancePolicy,
    ModelPieceEvidence,
    ModelPieceRecordRequest,
    holder_host,
    holder_id,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_order_advance_effect.handlers.handler_order_advance import (
    HandlerOrderAdvance,
    OrderWriterBackendNotBuiltError,
    build_records,
)
from omnimarket.nodes.node_order_advance_effect.handlers.records_in_memory import (
    InMemoryPieceRecords,
)
from omnimarket.nodes.node_order_advance_effect.overlay import (
    load_order_advance_policy,
)
from omnimarket.nodes.node_order_advance_effect.tests.support import (
    GATE_POLICY,
    HOST_A,
    HOST_B,
    KEPT_READING,
    ORDER,
    POLICY,
    ROLLED_BACK_READING,
    SHOWN,
    SpyStore,
    StaleReadStore,
    make_handler,
    stale_read,
    take_over,
    tick,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from omnimarket.protocols.protocol_piece_records import ProtocolPieceRecords
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

A = EnumOrderAdvanceOutcome


def read_position(store: ProtocolGateStore) -> int:
    return store.transact(
        ModelGateStoreRequest(
            correlation_id="read", operation=EnumGateStoreOperation.READ
        )
    ).order_position


def record_state(records: ProtocolPieceRecords, piece: str) -> EnumPieceState | None:
    rec = records.transact(
        ModelPieceRecordRequest(operation=EnumPieceRecordOperation.READ, piece=piece)
    ).record
    return rec.state if rec is not None else None


def drive_alpha_to_comparison(handler: HandlerOrderAdvance) -> None:
    """Claim the holder and bring alpha to ``in_comparison`` (live at t=10)."""
    assert handler.handle(take_over("w1", HOST_A, 0)).outcome is A.TAKEN_OVER
    assert handler.handle(tick(now=0)).piece_state is EnumPieceState.STARTED
    assert (
        handler.handle(tick(now=10, evidence=SHOWN)).piece_state is EnumPieceState.LIVE
    )
    assert handler.handle(tick(now=11)).piece_state is EnumPieceState.IN_COMPARISON


# ---- AC1: advance and restart ----


def test_advance_and_restart_a_test_order_passes_every_state() -> None:
    store = InMemoryGateStore(GATE_POLICY)
    records = InMemoryPieceRecords()
    handler = make_handler(store, records)
    seen: list[EnumPieceState] = []

    def step(now: float, evidence: ModelPieceEvidence | None = None) -> None:
        answer = handler.handle(tick(now=now, evidence=evidence))
        if answer.piece_state is not None and answer.outcome is A.ADVANCED:
            seen.append(answer.piece_state)

    assert handler.handle(take_over("w1", HOST_A, 0)).outcome is A.TAKEN_OVER
    # alpha: started, held until shown, live, comparison, kept only after the window, then the order moves
    step(now=0)
    assert handler.handle(tick(now=1)).outcome is A.IDLE, (
        "not shown yet: it stays started"
    )
    step(now=10, evidence=SHOWN)
    step(now=11)
    held = handler.handle(tick(now=50, evidence=KEPT_READING))
    assert held.outcome is A.IDLE, "the comparison window has not run"
    assert read_position(store) == 0
    step(now=110, evidence=KEPT_READING)
    assert read_position(store) == 1
    assert record_state(records, "alpha") is EnumPieceState.KEPT
    # beta: rolled back the moment the comparison reads so, and a rolled-back piece holds the order
    step(now=120)
    step(now=121, evidence=SHOWN)
    step(now=122)
    step(now=123, evidence=ROLLED_BACK_READING)
    assert record_state(records, "beta") is EnumPieceState.ROLLED_BACK
    assert handler.handle(tick(now=200, evidence=KEPT_READING)).outcome is A.IDLE
    assert read_position(store) == 1
    assert set(seen) == set(EnumPieceState)
    assert ORDER[read_position(store)] == "beta"


def test_advance_and_restart_a_kill_between_kept_and_the_advance_is_finished_once() -> (
    None
):
    inner = InMemoryGateStore(GATE_POLICY)
    store = SpyStore(inner, fail_first_advance=True)
    records = InMemoryPieceRecords()
    drive_alpha_to_comparison(make_handler(store, records))
    killed = make_handler(store, records)
    with pytest.raises(RuntimeError, match="killed before the advance"):
        killed.handle(tick(now=110, evidence=KEPT_READING))
    assert record_state(records, "alpha") is EnumPieceState.KEPT
    assert read_position(store) == 0, "the kill left the order where it was"
    # a new process over the same stores
    restarted = make_handler(store, records)
    resumed = restarted.handle(tick(now=111, evidence=KEPT_READING))
    assert resumed.outcome is A.ADVANCED
    assert resumed.order_position == 1
    again = make_handler(store, records).handle(tick(now=112, evidence=KEPT_READING))
    assert again.piece == "beta", "the next tick works on the next piece"
    assert read_position(store) == 1
    assert store.committed_advances == 1


def test_advance_and_restart_after_a_finished_advance_no_second_advance() -> None:
    inner = InMemoryGateStore(GATE_POLICY)
    store = SpyStore(inner)
    records = InMemoryPieceRecords()
    drive_alpha_to_comparison(make_handler(store, records))
    assert (
        make_handler(store, records)
        .handle(tick(now=110, evidence=KEPT_READING))
        .order_position
        == 1
    )
    for n in range(3):  # three restarts, each ticking with the kept reading still shown
        restarted = make_handler(store, records)
        restarted.handle(tick(now=120 + n, evidence=KEPT_READING))
    assert store.committed_advances == 1
    assert read_position(store) == 1


def test_advance_and_restart_a_replayed_tick_writes_nothing_twice() -> None:
    store = InMemoryGateStore(GATE_POLICY)
    records = InMemoryPieceRecords()
    handler = make_handler(store, records)
    drive_alpha_to_comparison(handler)
    epoch_before = handler.handle(tick(now=12)).epoch
    first = records.transact(
        ModelPieceRecordRequest(operation=EnumPieceRecordOperation.READ, piece="alpha")
    ).record
    replay = records.transact(
        ModelPieceRecordRequest(
            operation=EnumPieceRecordOperation.WRITE,
            step_key="run1/alpha/live",
            piece="alpha",
            to_state=EnumPieceState.LIVE,
            holder=holder_id(HOST_A, "w1"),
            fence_token=epoch_before,
            now=999,
        )
    )
    assert replay.outcome.value == "duplicate"
    after = records.transact(
        ModelPieceRecordRequest(operation=EnumPieceRecordOperation.READ, piece="alpha")
    ).record
    assert after == first, "a replayed key wrote nothing"
    assert after is not None
    assert after.updated_at != 999


def test_advance_and_restart_the_order_completes_and_the_writer_then_idles() -> None:
    store = InMemoryGateStore(GATE_POLICY)
    handler = make_handler(store, InMemoryPieceRecords())
    handler.handle(take_over("w1", HOST_A, 0))
    clock = 0.0
    for _ in ORDER:
        for evidence in (None, SHOWN, None, KEPT_READING):
            handler.handle(tick(now=clock, evidence=evidence))
            clock += 1
        clock += 100
        handler.handle(tick(now=clock, evidence=KEPT_READING))
    assert read_position(store) == len(ORDER)
    assert handler.handle(tick(now=clock + 1)).outcome is A.COMPLETE


# ---- AC2: fence and placement ----


def test_fence_and_placement_a_second_writer_with_a_stale_epoch_is_refused() -> None:
    store = InMemoryGateStore(GATE_POLICY)
    first = make_handler(store, InMemoryPieceRecords())
    assert first.handle(take_over("w1", HOST_A, 0)).outcome is A.TAKEN_OVER
    second = make_handler(store, InMemoryPieceRecords())
    stale = second.handle(take_over("w2", HOST_B, 0))
    assert stale.outcome is A.REFUSED_FENCE
    assert stale.holder == holder_id(HOST_A, "w1"), "the holder is unchanged"
    fresh = second.handle(take_over("w2", HOST_B, stale.epoch))
    assert fresh.outcome is A.TAKEN_OVER
    assert fresh.holder == holder_id(HOST_B, "w2")


def test_fence_and_placement_the_displaced_writer_writes_nothing() -> None:
    store = InMemoryGateStore(GATE_POLICY)
    records = InMemoryPieceRecords()
    handler = make_handler(store, records)
    handler.handle(take_over("w1", HOST_A, 0))
    assert handler.handle(tick("w1", HOST_A, now=0)).outcome is A.ADVANCED
    took = handler.handle(take_over("w2", HOST_B, 1))
    assert took.outcome is A.TAKEN_OVER
    refused = handler.handle(tick("w1", HOST_A, now=10, evidence=SHOWN))
    assert refused.outcome is A.REFUSED_NOT_HOLDER
    assert record_state(records, "alpha") is EnumPieceState.STARTED
    assert (
        handler.handle(tick("w2", HOST_B, now=10, evidence=SHOWN)).outcome is A.ADVANCED
    )
    assert record_state(records, "alpha") is EnumPieceState.LIVE


def test_fence_and_placement_a_stale_read_is_refused_at_the_records() -> None:
    """A writer that read the store before the handover is stopped by its stale token, not by its read."""
    gate = InMemoryGateStore(GATE_POLICY)
    store = StaleReadStore(gate)
    records = InMemoryPieceRecords()
    handler = make_handler(store, records)
    handler.handle(take_over("w1", HOST_A, 0))
    handler.handle(tick("w1", HOST_A, now=0))
    old_epoch = handler.handle(tick("w1", HOST_A, now=0)).epoch
    handler.handle(take_over("w2", HOST_B, old_epoch))
    store.stale["c-stale"] = stale_read("c-stale", holder_id(HOST_A, "w1"), old_epoch)
    refused = handler.handle(tick("w1", HOST_A, now=10, evidence=SHOWN, corr="c-stale"))
    assert refused.outcome is A.REFUSED_FENCE
    assert record_state(records, "alpha") is EnumPieceState.STARTED


def test_fence_and_placement_a_stale_read_cannot_advance_the_order() -> None:
    gate = InMemoryGateStore(GATE_POLICY)
    store = SpyStore(StaleReadStore(gate))
    stale_store = store.inner
    assert isinstance(stale_store, StaleReadStore)
    records = InMemoryPieceRecords()
    handler = make_handler(store, records)
    drive_alpha_to_comparison(handler)
    # w1 records alpha kept, then loses the holder before it advances the order
    holder_epoch = handler.handle(tick(now=12)).epoch
    stale_store.stale["c-late"] = stale_read(
        "c-late", holder_id(HOST_A, "w1"), holder_epoch
    )
    handler.handle(take_over("w2", HOST_B, holder_epoch))
    refused = handler.handle(tick(now=110, evidence=KEPT_READING, corr="c-late"))
    assert refused.outcome is A.REFUSED_FENCE
    assert read_position(store) == 0
    assert store.committed_advances == 0


def test_fence_and_placement_the_fallback_never_takes_over_on_the_holders_host() -> (
    None
):
    store = InMemoryGateStore(GATE_POLICY)
    handler = make_handler(store, InMemoryPieceRecords())
    held = handler.handle(take_over("w1", HOST_A, 0))
    same_host = handler.handle(take_over("w2", HOST_A, held.epoch))
    assert same_host.outcome is A.REFUSED_PLACEMENT
    assert same_host.holder == holder_id(HOST_A, "w1")
    assert same_host.epoch == held.epoch, "a refused placement writes nothing"
    other_host = handler.handle(take_over("w2", HOST_B, held.epoch))
    assert other_host.outcome is A.TAKEN_OVER


def test_fence_and_placement_a_holder_string_names_its_host() -> None:
    assert holder_host(holder_id(HOST_B, "w9")) == HOST_B


def test_fence_and_placement_the_primary_returns_from_another_host_only_after_the_fallback() -> (
    None
):
    store = InMemoryGateStore(GATE_POLICY)
    handler = make_handler(store, InMemoryPieceRecords())
    handler.handle(take_over("w1", HOST_A, 0))
    fallback = handler.handle(take_over("w2", HOST_B, 1))
    back = handler.handle(take_over("w1", HOST_A, fallback.epoch))
    assert back.outcome is A.TAKEN_OVER, "different hosts, a fresh epoch"
    assert handler.handle(tick("w2", HOST_B, now=1)).outcome is A.REFUSED_NOT_HOLDER


# ---- backend and overlay ----


def test_a_backend_that_is_not_built_is_refused_not_answered_by_the_timer_store() -> (
    None
):
    policy = ModelOrderAdvancePolicy(
        run_engine_backend=EnumRunEngineBackend.TEMPORAL_SELF_HOSTED,
        comparison_seconds=100,
    )
    with pytest.raises(OrderWriterBackendNotBuiltError):
        build_records(policy)
    with pytest.raises(OrderWriterBackendNotBuiltError):
        HandlerOrderAdvance(
            InMemoryGateStore(GATE_POLICY), InMemoryPieceRecords(), policy, ORDER
        )
    assert POLICY.run_engine_backend is EnumRunEngineBackend.TIMER_STORE


def test_overlay_loads_every_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "order_advance",
        {"run_engine_backend": "timer_store", "comparison_seconds": 604800},
    )
    loaded = load_order_advance_policy()
    assert loaded.run_engine_backend is EnumRunEngineBackend.TIMER_STORE
    assert loaded.comparison_seconds == 604800


@pytest.mark.parametrize(
    "change",
    [
        {"run_engine_backend": "archon"},
        {"comparison_seconds": 0},
        {"unknown_key": 1},
    ],
    ids=["unknown_backend", "zero_window", "extra_key"],
)
def test_overlay_refuses_a_bad_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    fields: dict[str, object] = {
        "run_engine_backend": "timer_store",
        "comparison_seconds": 10,
        **change,
    }
    install_node_overlay(monkeypatch, tmp_path, "order_advance", fields)
    with pytest.raises(NodeOverlayError):
        load_order_advance_policy()
