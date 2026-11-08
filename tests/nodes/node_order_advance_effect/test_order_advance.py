# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The order writer: golden and error chain events, restart, fencing, overlay, and named breaks.

The chain tests publish the tick command on an in-memory bus and assert every event produced,
in order, under the command's correlation id (plan section 3.2). The last blocks inject named
breaks and require the same assertions to go red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_order_advance import (
    EnumOrderAdvanceOutcome,
    EnumOrderReadinessBackend,
    ModelOrderAdvanceRequest,
    ModelOrderWriterPolicy,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store import (
    build_store,
)
from omnimarket.nodes.node_order_advance_effect.handlers.handler_order_advance import (
    HandlerOrderAdvance,
    InMemoryOrderReadiness,
    OrderReadinessNotBuiltError,
    answer_command,
    build_readiness,
    load_topics,
)
from omnimarket.nodes.node_order_advance_effect.overlay import (
    load_order_writer_policy,
)
from omnimarket.nodes.node_order_advance_effect.runtime_order_advance import (
    main as tick_main,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from tests.node_overlay_support import install_node_overlay
from tests.nodes.node_gate_store_effect.contract_suite import POLICY

pytestmark = pytest.mark.unit

TOPICS = load_topics()
ADVANCED = TOPICS["order_advanced"]
FAILED = TOPICS["order_advance_failed"]
ORDER = POLICY.order
OUT = EnumOrderAdvanceOutcome

WRITER = ModelOrderWriterPolicy(
    holder="writer-a",
    run_key="order-a",
    run_engine_backend="timer_store",
    readiness_backend=EnumOrderReadinessBackend.IN_MEMORY,
    ready_pieces=("alpha", "beta", "gamma"),
    takes_over_from_other_holder=False,
)


def handler(
    store: ProtocolGateStore | None = None,
    policy: ModelOrderWriterPolicy = WRITER,
    cls: type[HandlerOrderAdvance] = HandlerOrderAdvance,
) -> HandlerOrderAdvance:
    return cls(
        store if store is not None else build_store(POLICY),
        InMemoryOrderReadiness(policy.ready_pieces),
        policy,
        ORDER,
    )


def tick(key: str, now: float = 0.0) -> bytes:
    return json.dumps({"correlation_id": f"chain-{key}", "now": now}).encode()


async def publish(
    payloads: list[bytes],
    h: HandlerOrderAdvance,
    answer: Callable[..., list[tuple[str, bytes]]] = answer_command,
) -> list[tuple[str, dict[str, Any]]]:
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    produced = 0

    async def on_command(message: Any) -> None:
        for topic, body in answer(message.value, h, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["order_advance_requested"], on_message=on_command, group_id="writer"
        )
        for topic in (ADVANCED, FAILED):
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"t-{topic}"
            )
        for payload in payloads:
            await bus.publish(
                TOPICS["order_advance_requested"], key=None, value=payload
            )
            produced += 1
            for _ in range(200):
                if len(seen) >= produced:
                    break
                await asyncio.sleep(0.005)
    finally:
        await bus.close()
    return seen


def assert_chain(
    events: list[tuple[str, dict[str, Any]]], expected: list[tuple[str, str, str]]
) -> None:
    assert len(events) == len(expected), (
        f"{len(events)} events, expected {len(expected)}"
    )
    for (topic, body), (want_topic, want_corr, want_what) in zip(
        events, expected, strict=True
    ):
        assert topic == want_topic, f"topic {topic}, expected {want_topic}"
        assert body["correlation_id"] == want_corr, "the correlation id was lost"
        got = body.get("outcome") or body.get("failure", "")
        assert str(got).startswith(want_what), f"{got} != {want_what}"


GOLDEN = [
    (ADVANCED, "chain-t1", "advanced"),
    (ADVANCED, "chain-t2", "advanced"),
    (ADVANCED, "chain-t3", "advanced"),
    (ADVANCED, "chain-t4", "complete"),
]


def test_contract_topics() -> None:
    assert ADVANCED == "onex.evt.omnimarket.order-advanced.v1"
    assert FAILED == "onex.evt.omnimarket.order-advance-failed.v1"
    assert (
        TOPICS["order_advance_requested"]
        == "onex.cmd.omnimarket.order-advance-requested.v1"
    )


def test_golden_chain_advances_the_test_order_then_reports_complete() -> None:
    events = asyncio.run(publish([tick(f"t{i}") for i in (1, 2, 3, 4)], handler()))
    assert_chain(events, GOLDEN)
    assert [e[1]["order_position"] for e in events] == [1, 2, 3, 3]
    assert events[0][1]["holder"] == "writer-a"


def test_error_chain_bad_payload_is_a_failure_event() -> None:
    events = asyncio.run(publish([b'{"now": "late"}'], handler()))
    assert_chain(events, [(FAILED, "unknown", "bad-payload")])


class FaultyStore:
    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        raise ConnectionError("the store is down")


def test_error_chain_store_down_is_a_failure_event_and_writes_nothing() -> None:
    events = asyncio.run(publish([tick("f1")], handler(FaultyStore())))
    assert_chain(events, [(FAILED, "chain-f1", "store-fault:ConnectionError")])


def test_not_ready_piece_waits_and_writes_nothing() -> None:
    policy = WRITER.model_copy(update={"ready_pieces": ("alpha",)})
    h = handler(policy=policy)
    events = asyncio.run(publish([tick(f"t{i}") for i in (1, 2, 3)], h))
    assert_chain(
        events,
        [
            (ADVANCED, "chain-t1", "advanced"),
            (ADVANCED, "chain-t2", "waiting"),
            (ADVANCED, "chain-t3", "waiting"),
        ],
    )
    assert events[1][1]["next_piece"] == "beta"
    assert events[2][1]["order_position"] == 1


def test_a_new_writer_object_continues_from_the_store() -> None:
    """Restart: the position lives in the store, not in the writer."""
    store = build_store(POLICY)
    first = handler(store)
    assert (
        first.handle(ModelOrderAdvanceRequest(correlation_id="a")).order_position == 1
    )
    again = handler(store)
    result = again.handle(ModelOrderAdvanceRequest(correlation_id="b"))
    assert (result.outcome, result.order_position) == (OUT.ADVANCED, 2)


def test_replayed_tick_key_writes_nothing_twice() -> None:
    store = build_store(POLICY)
    h = handler(store)
    h.handle(ModelOrderAdvanceRequest(correlation_id="a"))
    replay = store.transact(
        ModelGateStoreRequest(
            correlation_id="r",
            operation=EnumGateStoreOperation.ADVANCE_ORDER,
            step_key="order-a/advance/1",
            holder="writer-a",
            expected_epoch=0,
            order_position=1,
        )
    )
    assert replay.outcome.value == "duplicate"
    assert replay.order_position == 1


def test_a_stale_writer_is_fenced_and_writes_nothing() -> None:
    """Fencing: a handover by another writer between this one's read and write fences it."""
    store = build_store(POLICY)

    class Racing(HandlerOrderAdvance):
        def _write(self, request: Any, op: Any, key: str, **fields: Any) -> Any:
            if op is EnumGateStoreOperation.ADVANCE_ORDER:
                state = self._read(request)
                super()._write(
                    request,
                    EnumGateStoreOperation.HANDOVER,
                    "other/handover",
                    holder="writer-b",
                    expected_epoch=state.epoch,
                )
            return super()._write(request, op, key, **fields)

    result = handler(store, cls=Racing).handle(
        ModelOrderAdvanceRequest(correlation_id="x")
    )
    assert result.outcome is OUT.FENCED
    assert result.order_position == 0
    assert result.holder == "writer-b"


def test_another_holder_is_not_taken_over_unless_the_overlay_says_so() -> None:
    store = build_store(POLICY)
    handler(store).handle(ModelOrderAdvanceRequest(correlation_id="a"))
    other = WRITER.model_copy(update={"holder": "writer-b", "run_key": "order-b"})
    refused = handler(store, other).handle(ModelOrderAdvanceRequest(correlation_id="b"))
    assert refused.outcome is OUT.REFUSED
    assert "writer-a" in refused.detail
    takeover = other.model_copy(update={"takes_over_from_other_holder": True})
    result = handler(store, takeover).handle(
        ModelOrderAdvanceRequest(correlation_id="c")
    )
    assert (result.outcome, result.holder, result.order_position) == (
        OUT.ADVANCED,
        "writer-b",
        2,
    )


FIELDS = {
    "holder": "writer-a",
    "run_key": "order-a",
    "run_engine_backend": "timer_store",
    "readiness_backend": "in_memory",
    "ready_pieces": ["alpha"],
    "takes_over_from_other_holder": False,
}


def test_overlay_loads_every_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(monkeypatch, tmp_path, "order_writer", FIELDS)
    policy = load_order_writer_policy()
    assert policy.holder == "writer-a"
    assert policy.run_engine_backend == "timer_store"
    assert policy.ready_pieces == ("alpha",)


@pytest.mark.parametrize("key", sorted(set(FIELDS) - {"ready_pieces"}))
def test_overlay_missing_a_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "order_writer",
        {k: v for k, v in FIELDS.items() if k != key},
    )
    with pytest.raises(NodeOverlayError):
        load_order_writer_policy()


def test_unbuilt_floor_reading_is_refused_not_answered_by_the_twin() -> None:
    policy = WRITER.model_copy(
        update={"readiness_backend": EnumOrderReadinessBackend.FLOOR_READING}
    )
    with pytest.raises(OrderReadinessNotBuiltError):
        build_readiness(policy)


def test_tick_cli_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert tick_main(["tick"]) == 3  # no overlay at all
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "gate_store",
        {
            "backend": "in_memory",
            "limits": {"alpha": 3, "beta": 2},
            "reservation_ttl_seconds": 60,
            "order": ["alpha", "beta"],
        },
    )
    install_node_overlay(monkeypatch, tmp_path, "order_writer", FIELDS)
    capsys.readouterr()
    assert tick_main(["tick"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "advanced"
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "order_writer",
        {**FIELDS, "readiness_backend": "floor_reading"},
    )
    assert tick_main(["tick"]) == 3


# ----- named breaks: each must turn the chain assertions red -----


class SkipsReadiness(HandlerOrderAdvance):
    """Break ``skip_readiness``: the order advances whether or not the piece reads ready."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._readiness = InMemoryOrderReadiness(tuple(ORDER))


class UnkeyedAdvance(HandlerOrderAdvance):
    """Break ``unkeyed_advance``: every write carries a fresh step key, so a replay writes twice."""

    _n = 0

    def _write(self, request: Any, op: Any, key: str, **fields: Any) -> Any:
        UnkeyedAdvance._n += 1
        return super()._write(request, op, f"{key}/{UnkeyedAdvance._n}", **fields)


def test_break_skip_readiness_goes_red() -> None:
    policy = WRITER.model_copy(update={"ready_pieces": ("alpha",)})
    broken = SkipsReadiness(
        build_store(POLICY), InMemoryOrderReadiness(()), policy, ORDER
    )
    events = asyncio.run(publish([tick(f"t{i}") for i in (1, 2, 3)], broken))
    with pytest.raises(AssertionError):
        assert_chain(
            events,
            [
                (ADVANCED, "chain-t1", "advanced"),
                (ADVANCED, "chain-t2", "waiting"),
                (ADVANCED, "chain-t3", "waiting"),
            ],
        )


def test_break_unkeyed_advance_goes_red() -> None:
    store = build_store(POLICY)
    h = handler(store, cls=UnkeyedAdvance)
    h.handle(ModelOrderAdvanceRequest(correlation_id="a"))
    replay = store.transact(
        ModelGateStoreRequest(
            correlation_id="r",
            operation=EnumGateStoreOperation.ADVANCE_ORDER,
            step_key="order-a/advance/1",
            holder="writer-a",
            expected_epoch=0,
            order_position=1,
        )
    )
    with pytest.raises(AssertionError):
        assert replay.outcome.value == "duplicate"
