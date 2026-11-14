# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The status line of the drain verb (OMN-24489): it says whether the host is safe to power down."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import FixedLaneSource
from omnimarket.nodes.node_host_drain_effect.adapters.stores import InMemoryDrainStore
from omnimarket.nodes.node_host_drain_effect.handlers.handler_host_drain import (
    HandlerHostDrain,
)
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumDrainOp,
    EnumDrainOutcome,
    ModelHostDrainRequest,
)
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostDrainOverlay

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


class _UnreadableLanes:
    """A lane source whose count cannot be read: an unknown count is never safe."""

    def running(self, host_name: str) -> int:
        raise OSError("the lane count command failed")


def _handler(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    lanes: Any,
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


def _expected_until(overlay: ModelHostDrainOverlay) -> str:
    until = NOW + timedelta(seconds=overlay.drain.default_ttl_seconds)
    return until.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_draining_with_lanes_running_is_not_safe(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    result = _handler(overlay, store, lanes, sink).handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="example-host-a")
    )
    assert result.outcome is EnumDrainOutcome.DRAINING
    assert result.draining is True
    assert result.lanes_running == 2
    assert result.drained is False
    assert result.status_line == (
        "example-host-a draining until "
        f"{_expected_until(overlay)}: 2 lane(s) running ({overlay.drain.lane_count_scope}): NOT SAFE to power down"
    )


def test_draining_with_no_lane_running_is_safe(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    result = _handler(overlay, store, lanes, sink).handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="example-host-b")
    )
    assert result.draining is True
    assert result.lanes_running == 0
    assert result.drained is True
    assert result.status_line == (
        "example-host-b draining until "
        f"{_expected_until(overlay)}: 0 lane(s) running ({overlay.drain.lane_count_scope}): SAFE to power down"
    )


def test_an_unreadable_lane_count_is_never_safe(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    sink: InMemoryEmissionSink,
) -> None:
    result = _handler(overlay, store, _UnreadableLanes(), sink).handle(
        ModelHostDrainRequest(op=EnumDrainOp.DRAIN, host_name="example-host-a")
    )
    assert result.draining is True
    assert result.lanes_running is None
    assert result.drained is False
    assert result.status_line == (
        "example-host-a draining until "
        f"{_expected_until(overlay)}: lane count unreadable: NOT SAFE to power down"
    )


def test_a_not_draining_host_says_so_with_its_lane_count(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    lanes: FixedLaneSource,
    sink: InMemoryEmissionSink,
) -> None:
    handler = _handler(overlay, store, lanes, sink)
    status = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.STATUS, host_name="example-host-a")
    )
    assert status.draining is False
    assert status.until is None
    assert (
        status.status_line
        == f"example-host-a not draining: 2 lane(s) running ({overlay.drain.lane_count_scope})"
    )
    cleared = handler.handle(
        ModelHostDrainRequest(op=EnumDrainOp.UNDRAIN, host_name="example-host-a")
    )
    assert (
        cleared.status_line
        == f"example-host-a not draining: 2 lane(s) running ({overlay.drain.lane_count_scope})"
    )


def test_a_not_draining_host_with_an_unreadable_count_says_so(
    overlay: ModelHostDrainOverlay,
    store: InMemoryDrainStore,
    sink: InMemoryEmissionSink,
) -> None:
    result = _handler(overlay, store, _UnreadableLanes(), sink).handle(
        ModelHostDrainRequest(op=EnumDrainOp.STATUS, host_name="example-host-a")
    )
    assert result.draining is False
    assert result.lanes_running is None
    assert result.status_line == "example-host-a not draining: lane count unreadable"
