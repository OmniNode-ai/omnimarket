# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_drain_effect <verb>`` (OMN-22905).

``drain --host H``       set H draining until its ttl (``--ttl-seconds``, else the overlay default): no new lane
                         is placed on it; running lanes finish.
``undrain --host H``     return H to placement.
``status --host H``      read whether H is draining, its running lanes, and whether it is safe to power down.
``check-start --host H`` ask whether a new lane may start on H (refused while draining).
``draining``             list the hosts now draining, one per line (for placement callers).
``runner-stop --runner R`` stop runner R only when it holds no job.

``drain``, ``undrain`` and ``status`` take ``--line`` to print the result's status line instead of the JSON.

Exit codes: 0 done; 1 refused or failed (a typed failure code is in the JSON); 3 a deployment fact is missing.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
)
from omnimarket.nodes.node_host_drain_effect.adapters.bus_drain import (
    AdvertisedLaneSource,
    BusAdvertisementSource,
    BusDrainCommandPublisher,
    SyncBus,
)
from omnimarket.nodes.node_host_drain_effect.adapters.host_reads import (
    CommandHostRead,
)
from omnimarket.nodes.node_host_drain_effect.adapters.runner_ports import (
    CommandWorkerProbe,
    CommandWorkerStopper,
    ProcWorkerProbe,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import (
    BusDrainStore,
    InMemoryDrainStore,
)
from omnimarket.nodes.node_host_drain_effect.handlers.handler_host_drain import (
    HandlerHostDrain,
)
from omnimarket.nodes.node_host_drain_effect.handlers.handler_idle_runner_stop import (
    HandlerIdleRunnerStop,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumDrainOutcome,
    EnumRunnerStopOutcome,
    ModelHostDrainRequest,
    ModelIdleRunnerStopRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import (
    ModelHostDrainOverlay,
    load_overlay,
)
from omnimarket.nodes.node_host_drain_effect.protocols import ProtocolDrainStore

_REFUSED = {
    EnumDrainOutcome.FAILED,
    EnumDrainOutcome.START_REFUSED,
}


class _MissingFactError(ValueError):
    """A deployment fact the profile needs is not in the environment."""


def _require_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise _MissingFactError(f"environment variable {name} is not set")
    return value


def open_sync_bus(overlay: ModelHostDrainOverlay) -> SyncBus:
    """The connection of the overlay's bus block: the lane's broker, or a bus inside this process for a drill."""
    from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus

    settings = overlay.bus
    if settings is None:
        raise _MissingFactError("the overlay declares no bus block")
    return SyncBus(
        lambda: open_lane_bus(
            bus=settings.kind,
            lane=settings.lane,
            kafka_bootstrap=settings.kafka_bootstrap,
            root=None,
        ),
        timeout_seconds=settings.timeout_seconds,
    )


def build_store_and_sink(
    overlay: ModelHostDrainOverlay,
    environ: Mapping[str, str],
    *,
    clock: Callable[[], datetime] | None = None,
    sync_bus: SyncBus | None = None,
) -> tuple[
    ProtocolDrainStore,
    ProtocolEmissionSink,
    Literal["in_memory", "real"],
    BusAdvertisementSource | None,
]:
    """The store and the chain-event sink of the overlay's profile, the backend label the chains carry, and the
    advertisement source the bus profile reads the hosts from (None for in_memory).

    The store reads the clock the caller gives (the same one the handler is built with), else the wall clock. The bus profile
    opens the bus unless the caller hands one in.
    """
    now = clock or (lambda: datetime.now(UTC))
    if overlay.profile == "in_memory":
        return InMemoryDrainStore(clock=now), InMemoryEmissionSink(), "in_memory", None
    settings = overlay.bus
    if settings is None:
        raise _MissingFactError("the bus profile declares no bus block")
    home = Path(_require_env(environ, overlay.emission.home_env))
    sink = JsonlEmissionSink(home / overlay.emission.journal_relpath)
    bus = sync_bus if sync_bus is not None else open_sync_bus(overlay)
    source = BusAdvertisementSource(
        bus,
        window_seconds=settings.window_seconds,
        cache_seconds=settings.cache_seconds,
    )
    store = BusDrainStore(BusDrainCommandPublisher(bus, source), source, clock=now)
    return store, sink, "real", source


class _NoLanes:
    """The in_memory profile has no host to read: an unreadable count, never a measured zero."""

    def running(self, host_name: str) -> int:
        raise ValueError(f"the in_memory profile reads no host ({host_name})")


def _print(model: BaseModel) -> None:
    print(model.model_dump_json())


def run(
    argv: Sequence[str],
    *,
    overlay: ModelHostDrainOverlay,
    environ: Mapping[str, str],
    sync_bus: SyncBus | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="node_host_drain_effect")
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in ("drain", "undrain", "status", "check-start"):
        p = sub.add_parser(verb)
        p.add_argument("--host", required=True)
        p.add_argument("--reason", default="")
        p.add_argument("--correlation-id", default="")
        if verb == "drain":
            p.add_argument("--ttl-seconds", type=int, default=None)
        if verb in ("drain", "undrain", "status"):
            p.add_argument("--line", action="store_true")
    sub.add_parser("draining")
    stop = sub.add_parser("runner-stop")
    stop.add_argument("--runner", required=True)
    stop.add_argument("--correlation-id", default="")
    args = parser.parse_args(list(argv))
    # One clock for the store and both handlers: the stores read a mark's expiry by it.

    def clock() -> datetime:
        return datetime.now(UTC)

    try:
        store, sink, backend, source = build_store_and_sink(
            overlay, environ, clock=clock, sync_bus=sync_bus
        )
    except _MissingFactError as exc:
        print(f"INDETERMINATE: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"INDETERMINATE: the bus could not be opened: {exc}", file=sys.stderr)
        return 3
    if args.verb == "draining":
        for host in store.draining_hosts():
            print(host)
        return 0
    if args.verb == "runner-stop":
        if overlay.runner_stop is None:
            print("INDETERMINATE: the overlay declares no runner_stop", file=sys.stderr)
            return 3
        stopper = HandlerIdleRunnerStop(
            overlay.runner_stop,
            probe=(
                CommandWorkerProbe(overlay.runner_stop)
                if overlay.runner_stop.probe_kind == "command"
                else ProcWorkerProbe(overlay.runner_stop)
            ),
            stopper=CommandWorkerStopper(overlay.runner_stop),
            store=store,
            sink=sink,
            clock=clock,
            backend=backend,
        )
        stopped = stopper.handle(
            ModelIdleRunnerStopRequest(
                runner_name=args.runner, correlation_id=args.correlation_id
            )
        )
        _print(stopped)
        return 0 if stopped.outcome is EnumRunnerStopOutcome.STOPPED else 1
    # The host's lanes and units are read from its own capacity advertisements, the same ones every launcher reads.
    lanes = AdvertisedLaneSource(source) if source is not None else _NoLanes()
    host_reads = tuple(CommandHostRead(r) for r in overlay.host_reads)
    drain = HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=lanes,
        sink=sink,
        clock=clock,
        backend=backend,
        host_reads=host_reads,
    )
    result = drain.handle(
        ModelHostDrainRequest(
            op=EnumDrainOp(args.verb.replace("-", "_")),
            host_name=args.host,
            correlation_id=args.correlation_id,
            reason=args.reason,
            ttl_seconds=getattr(args, "ttl_seconds", None),
        )
    )
    if getattr(args, "line", False):
        # A refused or failed command has no status line: its typed code and detail are the line.
        print(
            result.status_line
            or f"{result.host_name}: {result.failure_code}: {result.detail}"
        )
    else:
        _print(result)
    return 1 if result.outcome in _REFUSED else 0


def main(argv: Sequence[str] | None = None) -> int:
    environ = os.environ
    try:
        overlay = load_overlay(environ)
    except (NodeOverlayError, ValidationError) as exc:
        print(f"INDETERMINATE: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return run(sys.argv[1:] if argv is None else argv, overlay=overlay, environ=environ)
