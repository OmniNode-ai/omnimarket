# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain tests of the order writer (OMN-22938).

Written by hand in the shape of plan section 3.2: publish the commands on an in-memory bus, assert every event they
produce, in order, under each command's correlation id. The golden chains are a writer that claims the holder and
walks a piece to ``in_comparison``, and a piece kept after its comparison window that moves the order. The error chains
are a fallback refused on the holder's host, a stale take-over, a displaced writer, a stale read stopped at the records,
a payload that is no request, and a store that faults. Each ``test_named_break_chain_*`` injects one named mutation
and requires the same assertion that passes on the real code to go red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_gate_store import (
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_order_advance import (
    EnumPieceRecordOperation,
    ModelOrderAdvancePolicy,
    ModelOrderAdvanceRequest,
    ModelPieceEvidence,
    ModelPieceRecord,
    ModelPieceRecordRequest,
    ModelPieceRecordResult,
    holder_id,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_order_advance_effect.handlers import (
    handler_order_advance as handler_module,
)
from omnimarket.nodes.node_order_advance_effect.handlers.handler_order_advance import (
    HandlerOrderAdvance,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_order_advance_effect.handlers.records_in_memory import (
    InMemoryPieceRecords,
)
from omnimarket.nodes.node_order_advance_effect.tests.support import (
    GATE_POLICY,
    HOST_A,
    HOST_B,
    KEPT_READING,
    SHOWN,
    StaleReadStore,
    make_handler,
    stale_read,
    take_over,
    tick,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
ANSWERED = TOPICS["order_advance_answered"]
FAILED = TOPICS["order_advance_failed"]

Answer = Callable[[bytes, HandlerOrderAdvance, dict[str, str]], list[tuple[str, bytes]]]
Event = tuple[str, dict[str, Any]]
Want = tuple[str, str, str | None, dict[str, Any]]


def test_contract_topics_name_the_answered_and_failed_events() -> None:
    assert ANSWERED == "onex.evt.omnimarket.order-advance-answered.v1"
    assert FAILED == "onex.evt.omnimarket.order-advance-failed.v1"


def command(req: ModelOrderAdvanceRequest) -> bytes:
    return req.model_dump_json().encode()


async def publish(
    payloads: list[bytes],
    handler: HandlerOrderAdvance,
    answer: Answer = answer_command,
) -> list[Event]:
    """Publish each command on the in-memory bus; return every event produced, in order."""
    bus = EventBusInmemory()
    seen: list[Event] = []
    produced = 0

    async def on_command(message: Any) -> None:
        for topic, body in answer(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["order_advance_requested"],
            on_message=on_command,
            group_id="order-advance",
        )
        for topic in (ANSWERED, FAILED):
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"t-{topic}"
            )
        for payload in payloads:
            await bus.publish(
                TOPICS["order_advance_requested"], key=None, value=payload
            )
            produced += 1
            for _ in range(400):
                if len(seen) >= produced:
                    break
                await asyncio.sleep(0.005)
    finally:
        await bus.close()
    return seen


def assert_chain(events: list[Event], expected: list[Want]) -> None:
    """Each event, in order, is (topic, correlation id, outcome or failure prefix, extra fields)."""
    assert len(events) == len(expected), (
        f"{len(events)} events, expected {len(expected)}"
    )
    for (topic, body), (want_topic, want_corr, want_what, extra) in zip(
        events, expected, strict=True
    ):
        assert topic == want_topic, f"topic {topic}, expected {want_topic}"
        assert body["correlation_id"] == want_corr, "the correlation id was lost"
        got = body.get("outcome") or body.get("failure", "")
        assert want_what is None or str(got).startswith(want_what), (
            f"{got} != {want_what}"
        )
        for key, value in extra.items():
            assert body.get(key) == value, f"{key}: {body.get(key)} != {value}"


# ---- scenarios: one list of commands and the chain they must produce ----


def golden_walk() -> tuple[list[ModelOrderAdvanceRequest], list[Want]]:
    reqs = [
        take_over("w1", HOST_A, 0, corr="g1"),
        tick(now=0, corr="g2"),
        tick(now=10, evidence=SHOWN, corr="g3"),
        tick(now=11, corr="g4"),
    ]
    want: list[Want] = [
        (ANSWERED, "g1", "taken_over", {"holder": holder_id(HOST_A, "w1")}),
        (ANSWERED, "g2", "advanced", {"piece": "alpha", "piece_state": "started"}),
        (ANSWERED, "g3", "advanced", {"piece": "alpha", "piece_state": "live"}),
        (
            ANSWERED,
            "g4",
            "advanced",
            {"piece": "alpha", "piece_state": "in_comparison"},
        ),
    ]
    return reqs, want


def golden_kept() -> tuple[list[ModelOrderAdvanceRequest], list[Want]]:
    reqs = [
        tick(now=50, evidence=KEPT_READING, corr="k1"),
        tick(now=110, evidence=KEPT_READING, corr="k2"),
    ]
    want: list[Want] = [
        (ANSWERED, "k1", "idle", {"piece_state": "in_comparison", "order_position": 0}),
        (
            ANSWERED,
            "k2",
            "advanced",
            {"piece": "alpha", "piece_state": "kept", "order_position": 1},
        ),
    ]
    return reqs, want


def placement_and_fence() -> tuple[list[ModelOrderAdvanceRequest], list[Want]]:
    reqs = [
        take_over("w1", HOST_A, 0, corr="p1"),
        take_over("w2", HOST_A, 1, corr="p2"),
        take_over("w2", HOST_B, 0, corr="p3"),
        take_over("w2", HOST_B, 1, corr="p4"),
        tick("w1", HOST_A, now=0, corr="p5"),
    ]
    want: list[Want] = [
        (ANSWERED, "p1", "taken_over", {}),
        (ANSWERED, "p2", "refused_placement", {"holder": holder_id(HOST_A, "w1")}),
        (ANSWERED, "p3", "refused_fence", {"holder": holder_id(HOST_A, "w1")}),
        (ANSWERED, "p4", "taken_over", {"holder": holder_id(HOST_B, "w2")}),
        (ANSWERED, "p5", "refused_not_holder", {}),
    ]
    return reqs, want


def stale_writer() -> tuple[list[ModelOrderAdvanceRequest], list[Want]]:
    reqs = [
        take_over("w1", HOST_A, 0, corr="s1"),
        tick("w1", HOST_A, now=0, corr="s2"),
        take_over("w2", HOST_B, 1, corr="s3"),
        tick("w1", HOST_A, now=10, evidence=SHOWN, corr="c-stale"),
    ]
    want: list[Want] = [
        (ANSWERED, "s1", "taken_over", {}),
        (ANSWERED, "s2", "advanced", {"piece_state": "started"}),
        (ANSWERED, "s3", "taken_over", {"holder": holder_id(HOST_B, "w2")}),
        (ANSWERED, "c-stale", "refused_fence", {}),
    ]
    return reqs, want


def stale_handler(records: InMemoryPieceRecords | None = None) -> HandlerOrderAdvance:
    """A handler whose read for ``c-stale`` shows the holder it was before the handover."""
    store = StaleReadStore(InMemoryGateStore(GATE_POLICY))
    store.stale["c-stale"] = stale_read("c-stale", holder_id(HOST_A, "w1"), 1)
    return make_handler(store, records)


def run(
    handler: HandlerOrderAdvance,
    scenario: tuple[list[ModelOrderAdvanceRequest], list[Want]],
    answer: Answer = answer_command,
) -> tuple[list[Event], list[Want]]:
    reqs, want = scenario
    return asyncio.run(publish([command(r) for r in reqs], handler, answer)), want


def driven_to_comparison() -> HandlerOrderAdvance:
    """A handler whose holder already walked alpha to ``in_comparison`` (live at t=10)."""
    handler = make_handler()
    reqs, _ = golden_walk()
    for req in reqs:
        handler.handle(req)
    return handler


# ---- golden chains ----


def test_golden_chain_a_writer_claims_the_holder_and_walks_a_piece_to_comparison() -> (
    None
):
    assert_chain(*run(make_handler(), golden_walk()))


def test_golden_chain_a_kept_piece_moves_the_order_once_its_window_has_run() -> None:
    assert_chain(*run(driven_to_comparison(), golden_kept()))


# ---- error chains ----


def test_error_chain_a_fallback_on_the_holders_host_and_a_stale_epoch_are_refused() -> (
    None
):
    assert_chain(*run(make_handler(), placement_and_fence()))


def test_error_chain_a_stale_read_is_stopped_at_the_records() -> None:
    assert_chain(*run(stale_handler(), stale_writer()))


def test_error_chain_a_payload_that_is_no_request_is_a_typed_failure_event() -> None:
    events = asyncio.run(publish([b'{"nope": 1}'], make_handler()))
    assert_chain(events, [(FAILED, "unknown", "bad-payload", {})])


def test_error_chain_a_store_that_faults_is_a_typed_failure_event_not_a_crash() -> None:
    class FaultStore:
        def transact(self, req: ModelGateStoreRequest) -> ModelGateStoreResult:
            raise RuntimeError("store down")

    events = asyncio.run(
        publish(
            [command(take_over("w1", HOST_A, 0, corr="f1"))],
            make_handler(store=FaultStore()),
        )
    )
    assert_chain(events, [(FAILED, "f1", "store-fault:RuntimeError", {})])


# ---- named breaks: each mutation must turn its chain red ----


def test_named_break_chain_a_lost_correlation_id_turns_the_golden_chain_red() -> None:
    def broken(
        payload: bytes, handler: HandlerOrderAdvance, topics: dict[str, str]
    ) -> list[tuple[str, bytes]]:
        out = answer_command(payload, handler, topics)
        return [
            (t, json.dumps({**json.loads(b), "correlation_id": "lost"}).encode())
            for t, b in out
        ]

    events, want = run(make_handler(), golden_walk(), answer=broken)
    with pytest.raises(AssertionError, match="correlation id was lost"):
        assert_chain(events, want)


def test_named_break_chain_a_comparison_window_that_is_skipped_turns_the_kept_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = handler_module.plan_transition

    def skip_window(
        record: ModelPieceRecord | None,
        evidence: ModelPieceEvidence,
        now: float,
        policy: ModelOrderAdvancePolicy,
    ) -> Any:
        return real(
            record, evidence, now, policy.model_copy(update={"comparison_seconds": 1})
        )

    handler = driven_to_comparison()
    monkeypatch.setattr(handler_module, "plan_transition", skip_window)
    events, want = run(handler, golden_kept())
    with pytest.raises(AssertionError):
        assert_chain(events, want)


def test_named_break_chain_a_placement_check_that_is_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler_module, "holder_host", lambda _holder: "")
    events, want = run(make_handler(), placement_and_fence())
    with pytest.raises(AssertionError):
        assert_chain(events, want)


def test_named_break_chain_a_fence_that_is_not_checked_turns_the_stale_writer_chain_red() -> (
    None
):
    class LastWriterWinsRecords(InMemoryPieceRecords):
        """The mutation: a write adopts its own token first, so the newest token no longer fences anything."""

        def transact(self, request: ModelPieceRecordRequest) -> ModelPieceRecordResult:
            if (
                request.operation is EnumPieceRecordOperation.WRITE
                and request.holder is not None
                and request.fence_token is not None
            ):
                self._state.high_water = request.fence_token
                self._state.tokens[request.holder] = request.fence_token
            return super().transact(request)

    events, want = run(stale_handler(LastWriterWinsRecords()), stale_writer())
    with pytest.raises(AssertionError):
        assert_chain(events, want)


def test_named_break_chain_an_unfenced_records_twin_is_what_the_mutation_changed() -> (
    None
):
    """Positive control: the same stale scenario is green on the real records, so the break above is the mutation."""
    assert_chain(*run(stale_handler(InMemoryPieceRecords()), stale_writer()))
