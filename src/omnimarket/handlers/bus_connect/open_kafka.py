# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start a Kafka bus inside a deadline, then through the lane's declared second address (OMN-26027).

An alarm tick once hung 52 minutes because nothing bounded the connect to a blackholed broker
address, and the declared bootstrap was a tailnet name with one path. Each address now gets the
declared deadline; when one does not start, the next declared address is tried and the one that
connected is said on stderr. When none starts the caller gets a typed error that names every
address and why it failed, inside the sum of the deadlines.

A broker reached through its LAN address still advertises its tailnet name in its metadata, so for
the connection's life the advertised names are dialled at the address's host.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, Protocol

from omnibase_infra.errors import (
    InfraConnectionError,
    InfraTimeoutError,
    InfraUnavailableError,
)
from omnibase_infra.event_bus.lane_client_transport_binding import (
    bind_lane_client_transport,
)
from omnibase_infra.event_bus.model_lane_client_transport import (
    ModelLaneClientTransport,
)

from omnimarket.handlers.bus_connect.errors import (
    BusConnectDeadlineError,
    BusConnectError,
)
from omnimarket.models.bus_connect import (
    ModelBrokerFallback,
    ModelBusConnectAttempt,
    ModelBusConnectPolicy,
)

_STARTUP_FAILURES = (
    InfraConnectionError,
    InfraTimeoutError,
    InfraUnavailableError,
    OSError,
)


class ProtocolStartableBus(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...


def _consume_result(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()


@contextlib.contextmanager
def advertised_names_dialled_at(names: tuple[str, ...], address: str) -> Iterator[None]:
    """For this scope, resolve each advertised broker name as the host of ``address``."""
    if not names:
        yield
        return
    host = address.rpartition(":")[0]
    loop = asyncio.get_running_loop()
    original = loop.getaddrinfo

    async def remapped(name: Any, *args: Any, **kwargs: Any) -> Any:
        return await original(host if name in names else name, *args, **kwargs)

    patch_attribute = setattr
    patch_attribute(loop, "getaddrinfo", remapped)
    try:
        yield
    finally:
        delattr(loop, "getaddrinfo")


async def _start_within(
    bus: ProtocolStartableBus, policy: ModelBusConnectPolicy
) -> tuple[bool, str, set[asyncio.Future[Any]]]:
    """Start the bus: ``(timed_out, failure reason or "", tasks still winding down)``."""
    task = asyncio.ensure_future(bus.start())
    done, _ = await asyncio.wait({task}, timeout=policy.connect_deadline_seconds)
    if not done:
        task.cancel()
        task.add_done_callback(_consume_result)
        return True, f"no start inside {policy.connect_deadline_seconds:g}s", {task}
    try:
        task.result()
    except _STARTUP_FAILURES as exc:
        return False, f"{type(exc).__name__}: {exc}", set()
    return False, "", set()


async def _abandon(
    bus: ProtocolStartableBus,
    winding_down: set[asyncio.Future[Any]],
    policy: ModelBusConnectPolicy,
) -> None:
    """Close the failed bus and give it and its cancelled start one shared grace to finish."""
    closing = asyncio.ensure_future(bus.close())
    closing.add_done_callback(_consume_result)
    await asyncio.wait({closing, *winding_down}, timeout=policy.abandon_grace_seconds)


@contextlib.asynccontextmanager
async def open_kafka_bus(
    *,
    bootstrap: str,
    transport: ModelLaneClientTransport | None,
    fallbacks: tuple[ModelBrokerFallback, ...],
    policy: ModelBusConnectPolicy,
    make_bus: Callable[[str], ProtocolStartableBus],
) -> AsyncIterator[Any]:
    """Yield a started bus from the first declared address that starts inside the deadline.

    The lane transport is bound for the connection's life at the address it connected through, so
    the login is the lane's whichever address answered.
    """
    bootstrap_host = bootstrap.rpartition(":")[0]
    addresses: list[tuple[str, tuple[str, ...]]] = [(bootstrap, ())]
    for fallback in fallbacks:
        if fallback.address in {address for address, _ in addresses}:
            continue
        names = fallback.advertised_names
        if fallback.advertised_names_from_bootstrap and bootstrap_host:
            names = (*names, bootstrap_host)
        addresses.append((fallback.address, names))
    attempts: list[ModelBusConnectAttempt] = []
    for index, (address, names) in enumerate(addresses):
        async with contextlib.AsyncExitStack() as stack:
            if transport is not None:
                stack.enter_context(
                    bind_lane_client_transport(
                        transport.model_copy(update={"bootstrap_servers": address})
                    )
                )
            stack.enter_context(advertised_names_dialled_at(names, address))
            bus = make_bus(address)
            timed_out, reason, winding_down = await _start_within(bus, policy)
            if reason:
                await _abandon(bus, winding_down, policy)
                attempts.append(
                    ModelBusConnectAttempt(
                        address=address, timed_out=timed_out, reason=reason
                    )
                )
                continue
            if index:
                sys.stderr.write(
                    f"bus connect: {bootstrap} did not start ({attempts[0].reason}); "
                    f"connected through the declared second address {address}\n"
                )
            try:
                yield bus
            finally:
                await bus.close()
            return
    error = (
        BusConnectDeadlineError
        if any(a.timed_out for a in attempts)
        else BusConnectError
    )
    raise error(tuple(attempts))
