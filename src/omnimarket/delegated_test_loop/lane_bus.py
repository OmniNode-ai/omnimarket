# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Opening the bus the focused run travels on, and a synchronous bridge to it
(OMN-19458).

The broker address comes from the lane declaration (``--lane``), resolved and
authenticated exactly as ``onex delegate`` does it, or from an explicit
``--kafka-bootstrap``. Nothing is read from an ambient broker variable
(OMN-16871). ``inmemory`` is the offline bus: the lab-host side runs in the
same process.

The loop orchestrator is synchronous and calls its ``run`` port once per
focused run. :class:`LabRunBridge` owns one event loop on a thread that holds
the bus, the client and (in-memory only) the host for the whole loop, so a run
costs one publish and one terminal, not a broker connection and a group join.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Literal

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.delegated_test_loop.lab_run_bus import (
    FocusedRunBusCaller,
    FocusedRunHost,
    ProtocolFocusedRunHandler,
    ProtocolLabRunBus,
)
from omnimarket.nodes.node_push_validation_effect import (
    ModelFocusedTestRunReceipt,
    ModelFocusedTestRunRequest,
)

BusKind = Literal["kafka", "inmemory"]
_READY_TIMEOUT_SECONDS = 120


class LabRunBusError(RuntimeError):
    """The bus could not be selected or opened."""


@contextlib.asynccontextmanager
async def open_lab_run_bus(
    *,
    bus: BusKind,
    lane: str | None,
    kafka_bootstrap: str | None,
    omni_home: Path | None,
) -> AsyncIterator[ProtocolLabRunBus]:
    """Open and start the bus; close it on exit."""
    if bus == "inmemory":
        if lane is not None or kafka_bootstrap is not None:
            raise LabRunBusError(
                "--lane and --kafka-bootstrap name a broker; the in-memory bus has none"
            )
        memory = EventBusInmemory(environment="local", group="delegated-test-loop")
        await memory.start()
        try:
            yield memory
        finally:
            await memory.close()
        return

    # Imported here: the in-memory path must not need a Kafka client.
    from omnibase_infra.cli.delegate_lane import (
        DelegateLaneSelectionError,
        resolve_lane_target,
    )
    from omnibase_infra.cli.delegate_lane_credentials import (
        DelegateLaneCredentialError,
        resolve_lane_client_transport_for,
    )
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.lane_client_transport_binding import (
        bind_lane_client_transport,
    )

    try:
        target = resolve_lane_target(
            bus="kafka", lane=lane, kafka_bootstrap=kafka_bootstrap, omni_home=omni_home
        )
        transport = resolve_lane_client_transport_for(
            lane_target=target, onex_home=Path.home() / ".onex", environ=os.environ
        )
    except (DelegateLaneSelectionError, DelegateLaneCredentialError) as exc:
        raise LabRunBusError(str(exc)) from exc
    bootstrap = target.bootstrap_servers if target is not None else kafka_bootstrap
    if not bootstrap:
        raise LabRunBusError("no broker address was resolved")
    binding = (
        bind_lane_client_transport(transport)
        if transport is not None
        else contextlib.nullcontext()
    )
    with binding:
        kafka = EventBusKafka.from_bootstrap(bootstrap)
        await kafka.start()
        try:
            yield kafka
        finally:
            await kafka.close()


class LabRunBridge:
    """A synchronous ``run(request) -> receipt`` over the bus client.

    With ``host_handler`` set, a :class:`FocusedRunHost` serves the command
    topic on the same bus in the same loop: the offline form, and the form a
    lab host uses to run a loop on itself.
    """

    def __init__(
        self,
        open_bus: Callable[
            [], contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]
        ],
        *,
        host_handler: ProtocolFocusedRunHandler | None = None,
        wait_slack_seconds: int = 900,
    ) -> None:
        self._open_bus = open_bus
        self._host_handler = host_handler
        self._slack = wait_slack_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: FocusedRunBusCaller | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._thread_main, name="lab-run-bus", daemon=True
        )

    def __enter__(self) -> LabRunBridge:
        self._thread.start()
        if not self._ready.wait(_READY_TIMEOUT_SECONDS):
            raise LabRunBusError(
                f"the bus was not ready within {_READY_TIMEOUT_SECONDS}s"
            )
        if self._error is not None:
            raise LabRunBusError(
                f"{type(self._error).__name__}: {self._error}"
            ) from self._error
        return self

    def __exit__(self, *exc: object) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=60)

    def run(self, request: ModelFocusedTestRunRequest) -> ModelFocusedTestRunReceipt:
        if self._loop is None or self._client is None:
            raise LabRunBusError("the bridge is not open")
        future = asyncio.run_coroutine_threadsafe(self._client.run(request), self._loop)
        return future.result()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # fallback-ok: surfaced to the caller by __enter__
            self._error = exc
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with self._open_bus() as bus:
            host: FocusedRunHost | None = None
            if self._host_handler is not None:
                host = FocusedRunHost(bus, self._host_handler)
                await host.start()
            client = FocusedRunBusCaller(bus, wait_slack_seconds=self._slack)
            await client.start()
            self._client = client
            self._ready.set()
            try:
                await self._stop.wait()
            finally:
                await client.stop()
                if host is not None:
                    await host.stop()


__all__ = ["BusKind", "LabRunBridge", "LabRunBusError", "open_lab_run_bus"]
