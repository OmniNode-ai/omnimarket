# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus conformance suite (BUS): the rules every binding of the event bus protocol keeps.

The same checks run against the in-memory binding and against a recorded broker transcript. A
binding that fails one is not a binding. The mutation stubs in ``stubs/stub_bus.py`` each break
one of them: delivering out of order, delivering twice with no redelivery marker, or dropping a
message.
"""

from __future__ import annotations

from collections.abc import Callable

from omnibase_core.models.event_bus.model_event_message import ModelEventMessage

from omnimarket.nodes.node_binding_parity_orchestrator.conformance.suite_core import (
    Check,
    ModelCheckResult,
    expect,
    run_checks,
)
from omnimarket.nodes.node_binding_parity_orchestrator.protocols.protocol_local_bus import (
    ProtocolLocalBus,
)

type BusFactory = Callable[[str], ProtocolLocalBus]

TOPIC = "conformance.bus.main"
OTHER_TOPIC = "conformance.bus.other"
PAYLOADS = tuple(f"payload-{index}" for index in range(5))


async def _collect(
    bus: ProtocolLocalBus, topic: str, group: str, sink: list[ModelEventMessage]
) -> None:
    async def on_message(message: ModelEventMessage) -> None:
        sink.append(message)

    await bus.start()
    await bus.subscribe(topic, on_message=on_message, group_id=group)


async def _publish_all(bus: ProtocolLocalBus, topic: str) -> None:
    for payload in PAYLOADS:
        await bus.publish(topic, None, payload.encode("utf-8"))


def _values(sink: list[ModelEventMessage]) -> list[str]:
    return [message.value.decode("utf-8") for message in sink]


async def _check_publish_order(bus: ProtocolLocalBus) -> None:
    delivered: list[ModelEventMessage] = []
    await _collect(bus, TOPIC, "order", delivered)
    await _publish_all(bus, TOPIC)
    await bus.close()
    expect(
        _values(delivered) == list(PAYLOADS),
        f"delivered {_values(delivered)}, expected publish order {list(PAYLOADS)}",
    )


async def _check_no_unmarked_duplicates(bus: ProtocolLocalBus) -> None:
    delivered: list[ModelEventMessage] = []
    await _collect(bus, TOPIC, "dupes", delivered)
    await _publish_all(bus, TOPIC)
    await bus.close()
    unmarked = [m for m in delivered if m.headers.retry_count == 0]
    seen = _values(unmarked)
    expect(
        len(seen) == len(set(seen)),
        f"a message was delivered twice with no redelivery marker: {seen}",
    )


async def _check_every_message_delivered(bus: ProtocolLocalBus) -> None:
    delivered: list[ModelEventMessage] = []
    await _collect(bus, TOPIC, "all", delivered)
    await _publish_all(bus, TOPIC)
    await bus.close()
    missing = sorted(set(PAYLOADS) - set(_values(delivered)))
    expect(not missing, f"messages never delivered: {missing}")


async def _check_offsets_increase(bus: ProtocolLocalBus) -> None:
    delivered: list[ModelEventMessage] = []
    await _collect(bus, TOPIC, "offsets", delivered)
    await _publish_all(bus, TOPIC)
    await bus.close()
    offsets = [int(m.offset) for m in delivered if m.offset is not None]
    expect(
        len(offsets) == len(delivered) and offsets == sorted(set(offsets)),
        f"offsets are not strictly increasing: {offsets}",
    )


async def _check_topics_are_isolated(bus: ProtocolLocalBus) -> None:
    main: list[ModelEventMessage] = []
    other: list[ModelEventMessage] = []
    await _collect(bus, TOPIC, "iso-main", main)
    await _collect(bus, OTHER_TOPIC, "iso-other", other)
    await _publish_all(bus, TOPIC)
    await bus.close()
    expect(not other, f"a message of {TOPIC} reached a subscriber of {OTHER_TOPIC}")


async def _check_unsubscribe_stops_delivery(bus: ProtocolLocalBus) -> None:
    delivered: list[ModelEventMessage] = []

    async def on_message(message: ModelEventMessage) -> None:
        delivered.append(message)

    await bus.start()
    unsubscribe = await bus.subscribe(TOPIC, on_message=on_message, group_id="unsub")
    await unsubscribe()
    await _publish_all(bus, TOPIC)
    await bus.close()
    expect(not delivered, "an unsubscribed callback still received messages")


BUS_CHECKS: tuple[Check[ProtocolLocalBus], ...] = (
    ("delivers_in_publish_order", _check_publish_order),
    ("delivers_each_message_once", _check_no_unmarked_duplicates),
    ("delivers_every_message", _check_every_message_delivered),
    ("offsets_strictly_increase", _check_offsets_increase),
    ("topics_are_isolated", _check_topics_are_isolated),
    ("unsubscribe_stops_delivery", _check_unsubscribe_stops_delivery),
)


async def run_bus_suite(make_bus: BusFactory) -> tuple[ModelCheckResult, ...]:
    """Run the bus suite against bindings built by ``make_bus(check_name)``, one fresh bus per check."""
    return await run_checks(BUS_CHECKS, make_bus)
