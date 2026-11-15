# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain verb (OMN-22905): a host leaves placement by a command, running lanes finish, new starts are refused."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.bus_drain import (
    AdvertisedLaneSource,
)
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import (
    DroppingDrainStore,
    FixedHostRead,
    FixedLaneSource,
    LoopbackBus,
    make_bus_store,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import (
    InMemoryDrainStore,
)
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
from omnimarket.nodes.node_host_drain_effect.protocols import ProtocolDrainStore

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


def _handler(
    overlay: ModelHostDrainOverlay,
    store: ProtocolDrainStore,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> HandlerHostDrain:
    return HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=lanes,
        sink=sink,
        clock=lambda: NOW,
        backend="in_memory",
    )


def test_drain_sets_host_draining_and_running_lanes_finish(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    handler = _handler(overlay, store, lanes, sink)
    result = handler.handle(
        ModelHostDrainRequest(
            op=EnumDrainOp.DRAIN, host_name="example-host-a", correlation_id="c1"
        )
    )
    assert result.outcome is EnumDrainOutcome.DRAINING
    assert result.draining is True
    # The two lanes running on the host are counted, not stopped: the drain waits for them.
    assert result.lanes_running == 2
    assert result.drained is False
    assert store.is_draining("example-host-a") is True
    assert lanes.stopped == []


def test_start_on_draining_host_is_refused(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    handler = _handler(overlay, store, lanes, sink)
    handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="example-host-a")
    )
    refused = handler.handle(
        ModelHostDrainRequest(
            op=EnumDrainOp.CHECK_START, host_name="example-host-a", correlation_id="s1"
        )
    )
    assert refused.outcome is EnumDrainOutcome.START_REFUSED
    assert refused.admitted is False
    assert "draining" in refused.detail
    other = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.CHECK_START, host_name="example-host-b")
    )
    assert other.outcome is EnumDrainOutcome.START_ADMITTED
    assert other.admitted is True


def test_drained_reads_true_once_the_last_lane_finishes(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    bus = LoopbackBus(
        hosts=["example-host-a", "example-host-b"],
        clock=lambda: NOW,
        lanes={"example-host-a": 2, "example-host-b": 0},
    )
    store = make_bus_store(bus, clock=lambda: NOW)
    handler = HandlerHostDrain(
        overlay.drain,
        store=store,
        lanes=AdvertisedLaneSource(bus),
        sink=sink,
        clock=lambda: NOW,
        backend="real",
        host_reads=[FixedHostRead("model_servers", EnumHostReadKind.MODEL_SERVERS, {})],
    )
    handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="example-host-a")
    )
    bus.lanes["example-host-a"] = 0
    # Two advertisements per host in the window: the second one carries the updated lane count.
    bus.advertise()
    bus.advertise()
    status = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.STATUS, host_name="example-host-a")
    )
    assert status.draining is True
    assert status.drained is True


def test_undrain_returns_the_host_to_placement(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    handler = _handler(overlay, store, lanes, sink)
    handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="example-host-a")
    )
    cleared = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.UNDRAIN, host_name="example-host-a")
    )
    assert cleared.outcome is EnumDrainOutcome.CLEARED
    again = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.CHECK_START, host_name="example-host-a")
    )
    assert again.admitted is True


def test_drain_of_a_host_the_overlay_does_not_declare_is_refused(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    handler = _handler(overlay, store, lanes, sink)
    result = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="not-a-host")
    )
    assert result.outcome is EnumDrainOutcome.FAILED
    assert result.failure_code == overlay.drain.codes.unknown_host
    assert store.is_draining("not-a-host") is False


def test_injected_break_a_store_that_drops_the_write_fails_the_drain_by_readback(
    overlay: ModelHostDrainOverlay,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    """The failing control: the injected break the overlay names makes the reading go red, not green."""
    assert overlay.drain.injected_fault
    handler = _handler(overlay, DroppingDrainStore(), lanes, sink)
    result = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="example-host-a")
    )
    assert result.outcome is EnumDrainOutcome.FAILED
    assert result.failure_code == overlay.drain.codes.readback_failed
    assert result.draining is False
    # And a start is therefore not refused by a drain that never took: the reading says so, loudly.
    assert any(
        e.failure_code == overlay.drain.codes.readback_failed for e in sink.events
    )


# The JSON-lines file store is gone: drain state now rides the bus's capacity advertisements.


@pytest.mark.parametrize("op", [EnumDrainOp.DRAIN, EnumDrainOp.UNDRAIN])
def test_drain_and_undrain_are_idempotent(
    op: EnumDrainOp,
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    handler = _handler(overlay, store, lanes, sink)
    first = handler.handle(ModelHostDrainRequest(op=op, host_name="example-host-a"))
    second = handler.handle(ModelHostDrainRequest(op=op, host_name="example-host-a"))
    assert first.outcome is second.outcome
