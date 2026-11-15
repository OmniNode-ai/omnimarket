# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The status line of the drain verb (OMN-24489): SAFE is a reading of the host itself.

SAFE to power down needs the host's lanes, units and model servers to read 0, twice, on a drain mark that
outlives the process. A count above 0, a read that failed, a host with no host read declared and a mark held in
memory are each NOT SAFE. Lanes and units come from the host's own advertisement (two advertisements in the
window, each carrying a lanes read); a host that advertised once, or that never read its lanes, or that is silent
is NOT SAFE. A HELD host reads HELD, not failed and not SAFE.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.bus_drain import (
    AdvertisedLaneSource,
)
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import (
    FixedHostRead,
    LoopbackBus,
    make_bus_store,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import InMemoryDrainStore
from omnimarket.nodes.node_host_drain_effect.handlers.handler_host_drain import (
    HandlerHostDrain,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumDrainOutcome,
    EnumHostReadKind,
    ModelHostDrainRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostDrainOverlay
from omnimarket.nodes.node_host_drain_effect.protocols import (
    ProtocolDrainStore,
    ProtocolHostRead,
)

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
HOST = "example-host-b"


def _reads(
    *,
    model_servers: int = 0,
    unreadable: frozenset[str] = frozenset(),
) -> list[ProtocolHostRead]:
    return [
        FixedHostRead(
            EnumHostReadKind.MODEL_SERVERS.value,
            EnumHostReadKind.MODEL_SERVERS,
            {HOST: model_servers, "example-host-a": model_servers},
            unreadable=unreadable,
        )
    ]


def _bus(
    *,
    lanes: dict[str, int | None] | None = None,
    units: dict[str, int] | None = None,
    silent: bool = False,
    per_host: int = 2,
    held: dict[str, str] | None = None,
) -> LoopbackBus:
    return LoopbackBus(
        [HOST],
        clock=lambda: NOW,
        lanes=lanes if lanes is not None else {HOST: 0},
        units=units if units is not None else {HOST: 0},
        silent=frozenset([HOST]) if silent else frozenset(),
        advertisements_per_host=per_host,
        held=held if held is not None else {},
    )


def _handler(
    overlay: ModelHostDrainOverlay,
    store: ProtocolDrainStore,
    lanes: Any,
    sink: InMemoryEmissionSink,
    host_reads: Sequence[ProtocolHostRead],
    clock: Callable[[], datetime] = lambda: NOW,
) -> HandlerHostDrain:
    return HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=lanes,
        sink=sink,
        clock=clock,
        backend="real",
        host_reads=host_reads,
    )


def _drain(handler: HandlerHostDrain, host: str = HOST) -> Any:
    return handler.handle(ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name=host))


def _until(overlay: ModelHostDrainOverlay) -> str:
    return (NOW + timedelta(seconds=overlay.drain.default_ttl_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_every_read_zero_on_a_durable_mark_is_safe(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    bus = _bus()
    result = _drain(
        _handler(
            overlay,
            make_bus_store(bus, clock=lambda: NOW),
            AdvertisedLaneSource(bus),
            sink,
            _reads(),
        )
    )
    assert result.outcome is EnumDrainOutcome.DRAINING
    assert result.safe is True
    assert result.drained is True
    assert result.status_line == (
        f"{HOST} draining until {_until(overlay)}: 0 lane(s) running "
        f"({overlay.drain.lane_count_scope}); host read model_servers=0: SAFE to power down"
    )


def test_a_busy_host_is_not_safe(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    """The host's own advertisement shows work, so the host is not safe to power down."""
    bus = _bus(units={HOST: 3})
    result = _drain(
        _handler(
            overlay,
            make_bus_store(bus, clock=lambda: NOW),
            AdvertisedLaneSource(bus),
            sink,
            _reads(),
        )
    )
    assert result.safe is False
    assert result.drained is False
    assert result.status_line.endswith(": NOT SAFE to power down")


def test_an_unreadable_host_read_is_never_safe(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    bus = _bus()
    result = _drain(
        _handler(
            overlay,
            make_bus_store(bus, clock=lambda: NOW),
            AdvertisedLaneSource(bus),
            sink,
            _reads(unreadable=frozenset({HOST})),
        )
    )
    assert result.safe is False
    assert all(read.count is None for read in result.host_reads)
    assert "model_servers=unreadable" in result.status_line
    assert result.status_line.endswith(": NOT SAFE to power down")


def test_a_host_with_no_host_read_declared_is_not_safe(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    bus = _bus()
    result = _drain(
        _handler(
            overlay,
            make_bus_store(bus, clock=lambda: NOW),
            AdvertisedLaneSource(bus),
            sink,
            (),
        )
    )
    assert result.safe is False
    assert "host read none declared" in result.status_line
    assert "the host itself was not read" in result.detail


def test_a_mark_held_in_memory_is_not_safe(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    """The in_memory profile's mark dies with the process: a SAFE line on it would outlive its own evidence."""
    bus = _bus()
    result = _drain(
        _handler(
            overlay,
            InMemoryDrainStore(clock=lambda: NOW),
            AdvertisedLaneSource(bus),
            sink,
            _reads(),
        )
    )
    assert result.draining is True
    assert result.safe is False
    assert result.status_line.endswith(": NOT SAFE to power down")
    assert "held in memory only" in result.detail


def test_a_host_that_advertised_only_once_is_not_safe(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    """The lane source needs two advertisements in the window; one is not a measured zero."""
    bus = _bus(per_host=1)
    result = _drain(
        _handler(
            overlay,
            make_bus_store(bus, clock=lambda: NOW),
            AdvertisedLaneSource(bus),
            sink,
            _reads(),
        )
    )
    assert result.safe is False
    assert result.status_line.endswith(": NOT SAFE to power down")


def test_a_host_that_never_read_its_lanes_is_not_safe(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    """A lanes read of None is an unknown count, never a measured zero."""
    bus = _bus(lanes={HOST: None})
    result = _drain(
        _handler(
            overlay,
            make_bus_store(bus, clock=lambda: NOW),
            AdvertisedLaneSource(bus),
            sink,
            _reads(),
        )
    )
    assert result.safe is False
    assert result.status_line.endswith(": NOT SAFE to power down")


def test_a_silent_host_is_not_safe(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    """A host that advertised nothing has no mark and no count to read: the drain fails on its readback, never safe."""
    bus = _bus(silent=True)
    result = _drain(
        _handler(
            overlay,
            make_bus_store(bus, clock=lambda: NOW),
            AdvertisedLaneSource(bus),
            sink,
            _reads(),
        )
    )
    assert result.safe is False
    assert result.outcome is EnumDrainOutcome.FAILED
    assert result.failure_code == overlay.drain.codes.readback_failed


def test_a_held_host_reads_held_not_failed_not_safe(
    overlay: ModelHostDrainOverlay, sink: InMemoryEmissionSink
) -> None:
    """A host whose advertisement carries a capacity_refusal reads HELD, not failed and not SAFE."""
    bus = _bus(held={HOST: "capacity guard: at max units"})
    result = _drain(
        _handler(
            overlay,
            make_bus_store(bus, clock=lambda: NOW),
            AdvertisedLaneSource(bus),
            sink,
            _reads(),
        )
    )
    assert result.safe is False
    assert "HELD" in result.status_line
    assert "capacity guard: at max units" in result.status_line
    assert result.status_line.endswith(": NOT SAFE to power down")
