# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reading the cause-lease rows through the source the contract declares and the overlay selects.

``memory`` hands the rows the caller already holds to the handler. ``bus`` reads the contract's
topic through any ``ProtocolEventBus``-shaped bus (the in-memory bus or the Redpanda-backed one),
so the handler sees the same rows and takes the same decisions on both.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from omnimarket.nodes.node_third_lease_stop_compute.models.model_third_lease_stop import (
    ModelThirdLeaseStopConfig,
)
from omnimarket.nodes.node_third_lease_stop_compute.overlay import bus_source


class ProtocolRowBus(Protocol):
    """The two bus calls the row source uses; the in-memory and Kafka buses both provide them."""

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> Any: ...

    async def subscribe(
        self,
        topic: str,
        node_identity: Any = None,
        on_message: Callable[[Any], Awaitable[None]] | None = None,
        *,
        group_id: str | None = None,
    ) -> Callable[[], Awaitable[None]]: ...


def encode_row(row: str) -> bytes:
    """One ledger row as the bus message the contract's bus source reads."""
    return json.dumps({bus_source()["row_field"]: row}).encode()


def decode_row(value: bytes) -> str | None:
    """The row text of a bus message, or None when the message is not one."""
    try:
        body = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    row = body.get(bus_source()["row_field"]) if isinstance(body, dict) else None
    return row if isinstance(row, str) else None


async def publish_rows(bus: ProtocolRowBus, rows: Sequence[str]) -> None:
    """Put ledger rows on the contract's topic (what the ledger host's row mirror does)."""
    topic = bus_source()["topic"]
    for row in rows:
        await bus.publish(topic, None, encode_row(row))


async def read_rows(
    config: ModelThirdLeaseStopConfig,
    *,
    memory_rows: Sequence[str] = (),
    bus: ProtocolRowBus | None = None,
    expected: int = 0,
    group_id: str = "third-lease-stop",
    timeout_seconds: float = 30.0,
) -> tuple[str, ...]:
    """The rows of the source the config selects.

    ``bus`` reads the topic until ``expected`` rows arrived or the timeout ends; a source
    that delivered fewer rows than ``expected`` raises rather than deciding on a partial read.
    """
    if config.row_source == "memory":
        return tuple(memory_rows)
    if config.row_source != "bus":
        raise ValueError(
            f"third_lease_stop.row_source {config.row_source!r} has no reader"
        )
    if bus is None:
        raise ValueError("third_lease_stop.row_source is bus and no bus was supplied")
    rows: dict[str, None] = {}  # redelivery and replays of a topic add no row twice
    done = asyncio.Event()

    async def on_message(message: Any) -> None:
        row = decode_row(message.value)
        if row is not None:
            rows[row] = None
            if len(rows) >= expected:
                done.set()

    unsubscribe = await bus.subscribe(
        bus_source()["topic"], on_message=on_message, group_id=group_id
    )
    # start_consuming blocks until shutdown on the in-memory bus and returns on the Kafka one;
    # run it as a task either way and stop it with the read.
    start_consuming = getattr(bus, "start_consuming", None)
    consumer = asyncio.create_task(start_consuming()) if start_consuming else None
    try:
        if expected:
            await asyncio.wait_for(done.wait(), timeout=timeout_seconds)
    except TimeoutError:
        raise TimeoutError(
            f"third_lease_stop bus source delivered {len(rows)} of {expected} rows"
        ) from None
    finally:
        if consumer is not None and not consumer.done():
            consumer.cancel()
        await unsubscribe()
    return tuple(rows)
